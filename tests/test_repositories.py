"""
Tests for GET /api/v1/repositories
(app/api/v1/repositories.py, app/services/github_repository_service.py,
app/services/repository_service.py).

GitHub API calls are mocked via monkeypatch on
github_repository_service.list_repositories_for_user — no real network
call is ever made.

Reuses the SAVEPOINT-based app_db_session/authed_client/client fixtures
from tests/test_auth.py (imported, not duplicated) since
repository_service also calls db.commit() for real — see
tests/test_auth.py's module docstring for why a plain db_session (from
tests/conftest.py) isn't used for these tests. Neither tests/conftest.py
nor tests/test_auth.py is modified by this file.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.repository import Repository
from app.services import github_repository_service, session_service
from app.services.auth_exceptions import GitHubAPIError
from app.utils.encryption import encrypt_value
from tests.conftest import make_repository, make_user
from tests.test_auth import app_db_session, authed_client, client  # noqa: F401


def _github_repo(
    *,
    github_repo_id: int,
    name: str = "sample-repo",
    private: bool = False,
    default_branch: str = "main",
) -> dict:
    return {
        "github_repo_id": github_repo_id,
        "name": name,
        "full_name": f"test-user/{name}",
        "private": private,
        "default_branch": default_branch,
        "clone_url": f"https://github.com/test-user/{name}.git",
        "github_updated_at": datetime.now(timezone.utc),
    }


def _login(authed_client, app_db_session, user) -> str:
    raw_token, _session = session_service.create_session(app_db_session, user)
    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    return raw_token


# 1. Authenticated user can fetch repositories.
def test_authenticated_user_can_fetch_repositories(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1001)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=5001, name="alpha")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "alpha"


# 2. Repository list uses the authenticated user's GitHub token.
def test_repository_list_uses_authenticated_users_token(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1002)
    user.github_access_token_encrypted = encrypt_value("user-specific-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    seen_tokens = []

    async def _fake_list(access_token, *, page, per_page):
        seen_tokens.append(access_token)
        return []

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 200
    assert seen_tokens == ["user-specific-token"]


# 3. Returned repositories are wrapped in {"data": [...]}.
def test_response_is_wrapped_in_data(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1003)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return []

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 200
    body = response.json()
    assert list(body.keys()) == ["data", "meta"]
    assert isinstance(body["data"], list)


# 4. Pagination parameters work.
def test_pagination_parameters_are_forwarded(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1004)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    seen_params = {}

    async def _fake_list(access_token, *, page, per_page):
        seen_params["page"] = page
        seen_params["per_page"] = per_page
        return []

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories", params={"page": 2, "per_page": 50})

    assert response.status_code == 200
    assert seen_params == {"page": 2, "per_page": 50}


# 5. Invalid pagination values are rejected.
@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"per_page": 0},
        {"per_page": 101},
    ],
)
def test_invalid_pagination_values_are_rejected(authed_client, app_db_session, params):
    user = make_user(app_db_session, github_id=1005)
    _login(authed_client, app_db_session, user)

    response = authed_client.get("/api/v1/repositories", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# 6. New GitHub repositories are inserted into the local repositories table.
def test_new_repositories_are_inserted(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1006)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=6001, name="new-repo")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")
    assert response.status_code == 200

    row = app_db_session.execute(
        select(Repository).where(Repository.user_id == user.id, Repository.github_repo_id == 6001)
    ).scalar_one()
    assert row.name == "new-repo"


# 7. Existing repositories are updated rather than duplicated.
# 8. Same GitHub repository does not create duplicate rows for the same user.
def test_existing_repository_is_updated_not_duplicated(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1007)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    existing = make_repository(app_db_session, user, github_repo_id=7001)
    original_id = existing.id
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=7001, name="renamed-repo")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")
    assert response.status_code == 200

    rows = (
        app_db_session.execute(
            select(Repository).where(
                Repository.user_id == user.id, Repository.github_repo_id == 7001
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1  # (8) no duplicate
    assert rows[0].id == original_id  # (7) same row, updated in place
    assert rows[0].name == "renamed-repo"


# 9. Unauthenticated request is rejected.
def test_unauthenticated_request_is_rejected(authed_client):
    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 10. GitHub 401 is handled safely.
def test_github_401_is_handled_safely(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1010)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        raise GitHubAPIError("GitHub rejected the stored access token.")

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "GITHUB_API_ERROR"
    assert "token" not in body["error"]["message"].lower()


# 11. GitHub 403 is handled safely.
def test_github_403_is_handled_safely(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1011)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        raise GitHubAPIError("GitHub API access was forbidden or rate-limited.")

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GITHUB_API_ERROR"


# 12. GitHub 5xx/network failure is handled safely.
def test_github_5xx_or_network_failure_is_handled_safely(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1012)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        raise GitHubAPIError("GitHub API is currently unavailable.")

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GITHUB_API_ERROR"


# 13. GitHub access token never appears in the response.
def test_access_token_never_appears_in_response(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1013)
    user.github_access_token_encrypted = encrypt_value("super-secret-token-value")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=1313, name="repo")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 200
    assert "super-secret-token-value" not in response.text
    assert "github_access_token_encrypted" not in response.text


# 14. GitHub access token never appears in logs/errors produced by the implementation.
def test_access_token_never_appears_in_error_output(authed_client, app_db_session, monkeypatch, capsys):
    user = make_user(app_db_session, github_id=1014)
    user.github_access_token_encrypted = encrypt_value("another-secret-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        raise GitHubAPIError("GitHub API is currently unavailable.")

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 502
    assert "another-secret-token" not in response.text

    captured = capsys.readouterr()
    assert "another-secret-token" not in captured.out
    assert "another-secret-token" not in captured.err


# Bonus (directly requested in the Stage 4 adjustments): a DB write
# failure during sync rolls back cleanly and propagates as a safe,
# generic error response — no partial row is left behind.
def test_db_write_failure_during_sync_rolls_back_and_returns_500(
    authed_client, app_db_session, monkeypatch
):
    from fastapi.testclient import TestClient

    from app.api.v1 import repositories as repositories_module
    from app.main import app

    user = make_user(app_db_session, github_id=1099)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()

    # `authed_client` (still requested as a fixture above) sets the
    # get_db() override on the shared `app` object for this test's
    # duration, which is reused below. The HTTP call itself is made
    # through a separate, local TestClient with
    # raise_server_exceptions=False: this is the one test that
    # intentionally lets an unhandled exception reach Starlette's
    # ServerErrorMiddleware, and the shared client's default
    # (raise_server_exceptions=True) re-raises that exception into the
    # test instead of returning the 500 response a real HTTP client
    # would receive. This setting only affects the test harness — it has
    # no effect on the running application.
    raw_token, _session = session_service.create_session(app_db_session, user)
    local_client = TestClient(app, raise_server_exceptions=False)
    local_client.cookies.set(settings.session_cookie_name, raw_token)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=9901, name="will-fail")]

    def _boom(db, user, github_repos):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)
    monkeypatch.setattr(repositories_module.repository_service, "sync_repositories", _boom)

    response = local_client.get("/api/v1/repositories")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"

    leftover = app_db_session.execute(
        select(Repository).where(Repository.user_id == user.id, Repository.github_repo_id == 9901)
    ).scalar_one_or_none()
    assert leftover is None


# --- Contract-compliance fix: meta {page, per_page, has_next} (API_CONTRACT.md §3.1) ---


# 15. meta carries the default page/per_page when none are requested.
def test_response_includes_meta_with_default_pagination(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1101)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return [_github_repo(github_repo_id=8101, name="only-repo")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories")

    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 1, "per_page": 30, "has_next": False}


# 16. meta echoes the requested page/per_page.
def test_meta_echoes_requested_page_and_per_page(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=1102)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        return []

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    response = authed_client.get("/api/v1/repositories", params={"page": 3, "per_page": 50})

    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 3, "per_page": 50, "has_next": False}


# 17. has_next is true after a full page and false after a short page.
def test_meta_has_next_reflects_whether_github_returned_a_full_page(
    authed_client, app_db_session, monkeypatch
):
    user = make_user(app_db_session, github_id=1103)
    user.github_access_token_encrypted = encrypt_value("some-test-token")
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    async def _fake_list(access_token, *, page, per_page):
        if page == 1:
            return [
                _github_repo(github_repo_id=8201, name="repo-one"),
                _github_repo(github_repo_id=8202, name="repo-two"),
            ]
        return [_github_repo(github_repo_id=8203, name="repo-three")]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)

    first = authed_client.get("/api/v1/repositories", params={"page": 1, "per_page": 2})
    last = authed_client.get("/api/v1/repositories", params={"page": 2, "per_page": 2})

    assert first.status_code == 200
    assert first.json()["meta"] == {"page": 1, "per_page": 2, "has_next": True}
    assert last.status_code == 200
    assert last.json()["meta"] == {"page": 2, "per_page": 2, "has_next": False}
