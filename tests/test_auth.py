"""
Tests for GitHub OAuth + cookie-session authentication
(app/api/v1/auth.py, app/services/{github_oauth_service,user_service,
session_service}.py).

All GitHub HTTP calls are mocked via monkeypatch on
app.services.github_oauth_service — no real network call is ever made.

Requires a live PostgreSQL test database, exactly like
tests/test_database_layer.py (see DATABASE_SETUP.md) — these tests are
skipped the same way (via the existing test_engine fixture) if one isn't
reachable.

Why a separate DB fixture from tests/conftest.py's db_session:
this stage's services call db.commit() for real (required so
last_used_at etc. actually reach PostgreSQL, not just the in-memory
object). tests/conftest.py's db_session binds directly to a connection
with one outer transaction — a real commit() there would end that
transaction early. app_db_session below uses the standard SAVEPOINT
"join the session to an external transaction" recipe instead, so
application-level commits only end a SAVEPOINT, and the true rollback
still happens once, at the end of the test. tests/conftest.py itself is
untouched.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.session import Session as SessionModel
from app.models.user import User
from app.services import github_oauth_service, session_service
from app.services.auth_exceptions import GitHubOAuthError
from app.utils.encryption import decrypt_value
from tests.api_conftest import client  # noqa: F401  (fixture import)
from tests.conftest import make_user


@pytest.fixture()
def app_db_session(test_engine):
    connection = test_engine.connect()
    outer_transaction = connection.begin()

    SessionFactory = sessionmaker(bind=connection, future=True)
    session = SessionFactory()

    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture()
def authed_client(client, app_db_session):
    """TestClient wired so the app's get_db() dependency resolves to the
    same SAVEPOINT-isolated session used for test setup/assertions."""
    app.dependency_overrides[get_db] = lambda: app_db_session
    yield client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def oauth_configured(monkeypatch):
    """Fake-but-valid-shaped GitHub OAuth config, isolated per test."""
    monkeypatch.setattr(settings, "github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings, "github_redirect_uri", "http://localhost:8000/api/v1/auth/github/callback"
    )


def _mock_successful_github_calls(monkeypatch, *, github_id: int, username: str):
    async def _fake_exchange(code):
        return "fake-github-access-token"

    async def _fake_fetch_user(access_token):
        return {
            "github_id": github_id,
            "username": username,
            "avatar_url": "https://example.com/avatar.png",
        }

    monkeypatch.setattr(github_oauth_service, "exchange_code_for_token", _fake_exchange)
    monkeypatch.setattr(github_oauth_service, "fetch_github_user", _fake_fetch_user)


# 1. GitHub login endpoint generates OAuth state and redirects correctly.
def test_github_login_generates_state_and_redirects(client, oauth_configured):
    response = client.get("/api/v1/auth/github/login", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")

    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["test-client-id"]
    assert query["redirect_uri"] == ["http://localhost:8000/api/v1/auth/github/callback"]
    assert "state" in query
    assert len(query["state"][0]) > 20  # secrets.token_urlsafe(32) output

    set_cookie = response.headers.get("set-cookie", "")
    assert settings.oauth_state_cookie_name in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_github_login_returns_configuration_error_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", None)
    monkeypatch.setattr(settings, "github_client_secret", None)
    monkeypatch.setattr(settings, "github_redirect_uri", None)

    response = client.get("/api/v1/auth/github/login", follow_redirects=False)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"


# 2. OAuth callback rejects invalid state.
def test_callback_rejects_invalid_state(authed_client, oauth_configured):
    authed_client.cookies.set(settings.oauth_state_cookie_name, "cookie-state-value")

    response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "different-state-value"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "error=oauth_failed" in response.headers["location"]


def test_callback_rejects_missing_state_cookie(authed_client, oauth_configured):
    response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "some-state"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "error=oauth_failed" in response.headers["location"]


# 3. OAuth callback handles GitHub token exchange failure.
def test_callback_handles_token_exchange_failure(authed_client, oauth_configured, monkeypatch):
    authed_client.cookies.set(settings.oauth_state_cookie_name, "matching-state")

    async def _boom(code):
        raise GitHubOAuthError("token exchange failed")

    monkeypatch.setattr(github_oauth_service, "exchange_code_for_token", _boom)

    response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "matching-state"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "error=oauth_failed" in response.headers["location"]


# 4. Successful callback creates a new user.
# 5. Successful callback creates a session.
# 6. Session token is hashed in the database.
# 7. Raw session token is not stored.
def test_successful_callback_creates_user_and_hashed_session(
    authed_client, app_db_session, oauth_configured, monkeypatch
):
    _mock_successful_github_calls(monkeypatch, github_id=777222, username="new-user")
    authed_client.cookies.set(settings.oauth_state_cookie_name, "matching-state")

    response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "matching-state"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].rstrip("/").endswith("/dashboard")

    user = app_db_session.execute(
        select(User).where(User.github_id == 777222)
    ).scalar_one()  # (4) new user created
    assert user.username == "new-user"

    session_row = app_db_session.execute(
        select(SessionModel).where(SessionModel.user_id == user.id)
    ).scalar_one()  # (5) session created
    assert len(session_row.session_token_hash) == 64  # (6) sha256 hex digest, i.e. hashed

    raw_cookie_value = response.cookies.get(settings.session_cookie_name)
    assert raw_cookie_value is not None
    assert raw_cookie_value != session_row.session_token_hash  # (7) raw token not stored as-is
    assert raw_cookie_value not in session_row.session_token_hash


# 8. GET /auth/me returns authenticated user.
def test_get_me_returns_authenticated_user(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=888333)
    raw_token, _session = session_service.create_session(app_db_session, user)

    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    response = authed_client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["github_id"] == 888333
    assert body["data"]["username"] == user.username
    assert "github_access_token_encrypted" not in body["data"]


# 9. GET /auth/me rejects missing/invalid/expired session.
def test_get_me_rejects_missing_session(authed_client):
    response = authed_client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_get_me_rejects_invalid_session(authed_client):
    authed_client.cookies.set(settings.session_cookie_name, "not-a-real-token")
    response = authed_client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_get_me_rejects_expired_session(authed_client, app_db_session):
    import hashlib
    import secrets

    user = make_user(app_db_session, github_id=999444)
    raw_token = secrets.token_urlsafe(32)
    expired_session = SessionModel(
        user_id=user.id,
        session_token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    app_db_session.add(expired_session)
    app_db_session.flush()

    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    response = authed_client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 10. POST /auth/logout invalidates the session.
# 11. Logout clears the cookie.
def test_logout_invalidates_session_and_clears_cookie(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=111555)
    raw_token, session_row = session_service.create_session(app_db_session, user)
    session_id = session_row.id

    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    response = authed_client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json()["data"]["logged_out"] is True

    set_cookie_header = response.headers.get("set-cookie", "")
    assert settings.session_cookie_name in set_cookie_header
    assert "max-age=0" in set_cookie_header.lower() or "expires=" in set_cookie_header.lower()

    remaining = app_db_session.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    ).scalar_one_or_none()
    assert remaining is None  # (10) session invalidated/deleted


# 12. GitHub access token is stored encrypted and is not exposed by /auth/me.
def test_github_token_stored_encrypted_and_not_exposed(
    authed_client, app_db_session, oauth_configured, monkeypatch
):
    _mock_successful_github_calls(monkeypatch, github_id=222666, username="secure-user")
    authed_client.cookies.set(settings.oauth_state_cookie_name, "matching-state")

    callback_response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "matching-state"},
        follow_redirects=False,
    )
    assert callback_response.status_code == 302

    user = app_db_session.execute(
        select(User).where(User.github_id == 222666)
    ).scalar_one()

    assert user.github_access_token_encrypted != "fake-github-access-token"
    assert "fake-github-access-token" not in user.github_access_token_encrypted
    assert decrypt_value(user.github_access_token_encrypted) == "fake-github-access-token"

    raw_session_token = callback_response.cookies.get(settings.session_cookie_name)
    authed_client.cookies.set(settings.session_cookie_name, raw_session_token)
    me_response = authed_client.get("/api/v1/auth/me")

    assert "github_access_token_encrypted" not in me_response.json()["data"]
    assert "fake-github-access-token" not in me_response.text


# 13. Duplicate GitHub users are not created.
# 14. Existing GitHub user is updated rather than duplicated.
def test_existing_github_user_is_updated_not_duplicated(
    authed_client, app_db_session, oauth_configured, monkeypatch
):
    existing = make_user(app_db_session, github_id=333777)
    original_id = existing.id

    _mock_successful_github_calls(monkeypatch, github_id=333777, username="updated-username")
    authed_client.cookies.set(settings.oauth_state_cookie_name, "matching-state")

    response = authed_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "some-code", "state": "matching-state"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    matching_users = app_db_session.execute(
        select(User).where(User.github_id == 333777)
    ).scalars().all()

    assert len(matching_users) == 1  # (13) no duplicate
    assert matching_users[0].id == original_id  # (14) same row, updated in place
    assert matching_users[0].username == "updated-username"
