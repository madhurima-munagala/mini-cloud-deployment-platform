"""
Tests for GET/PUT /api/v1/repositories/{id}/env
(app/api/v1/repositories.py, app/services/environment_variable_service.py).

Deployment-level override creation/resolution is exercised together with
POST /deployments in tests/test_deployments.py (tests 28-31), since
overrides have no endpoint of their own here — see
environment_variable_service.py's module docstring for why.

Reuses the SAVEPOINT-based app_db_session/authed_client/client fixtures
from tests/test_auth.py (imported, not duplicated), since
environment_variable_service calls db.commit() for real. Neither
tests/conftest.py nor tests/test_auth.py is modified by this file.
"""

from app.core.config import settings
from app.models.environment_variable import EnvironmentVariable
from app.services import environment_variable_service, session_service
from app.services.environment_variable_service import InvalidEnvVarError
from app.utils.encryption import decrypt_value
from sqlalchemy import select
from tests.conftest import make_repository, make_user
from tests.test_auth import app_db_session, authed_client, client  # noqa: F401


def _login(authed_client, app_db_session, user) -> str:
    raw_token, _session = session_service.create_session(app_db_session, user)
    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    return raw_token


# 1. PUT sets defaults; GET returns them masked — including non-sensitive keys.
def test_put_then_get_returns_all_values_masked(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6001)
    repo = make_repository(app_db_session, user, github_repo_id=7001)
    _login(authed_client, app_db_session, user)

    put_response = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={
            "env_vars": [
                {"key": "PORT", "value": "3000"},           # non-sensitive
                {"key": "API_KEY", "value": "sk_live_xyz"},  # sensitive
            ]
        },
    )
    assert put_response.status_code == 200
    for item in put_response.json()["data"]:
        assert item["value"] == "\u2022" * 8  # masked even in the PUT response

    get_response = authed_client.get(f"/api/v1/repositories/{repo.id}/env")
    assert get_response.status_code == 200
    data = {item["key"]: item["value"] for item in get_response.json()["data"]}
    assert data["PORT"] == "\u2022" * 8       # non-sensitive: STILL masked (Decision 1)
    assert data["API_KEY"] == "\u2022" * 8    # sensitive: masked


# 2. PUT full-replace: a second PUT with a different key set drops the old keys.
def test_put_is_full_replace_not_merge(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6002)
    repo = make_repository(app_db_session, user, github_repo_id=7002)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}, {"key": "DEBUG", "value": "true"}]},
    )
    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "NODE_ENV", "value": "production"}]},
    )

    response = authed_client.get(f"/api/v1/repositories/{repo.id}/env")
    keys = {item["key"] for item in response.json()["data"]}
    assert keys == {"NODE_ENV"}  # PORT and DEBUG are gone, not merged


# 2b. PUT full-replace with the SAME key (PORT=3000 -> PORT=9090) succeeds and
#     leaves exactly one row for that key — regression for the unique-index
#     violation when DELETE and INSERT shared one flush.
def test_put_replaces_existing_key_with_same_key(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6012)
    repo = make_repository(app_db_session, user, github_repo_id=7011)
    _login(authed_client, app_db_session, user)

    first = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}]},
    )
    assert first.status_code == 200

    second = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "9090"}]},
    )
    assert second.status_code == 200
    assert [item["key"] for item in second.json()["data"]] == ["PORT"]

    rows = environment_variable_service.list_repository_defaults(app_db_session, repo.id)
    assert [row.key for row in rows] == ["PORT"]
    assert decrypt_value(rows[0].value_encrypted) == "9090"


# 2c. An empty PUT still clears every default.
def test_put_empty_list_clears_all_defaults(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6013)
    repo = make_repository(app_db_session, user, github_repo_id=7012)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}, {"key": "API_KEY", "value": "x"}]},
    )

    put_response = authed_client.put(f"/api/v1/repositories/{repo.id}/env", json={"env_vars": []})
    assert put_response.status_code == 200
    assert put_response.json()["data"] == []

    get_response = authed_client.get(f"/api/v1/repositories/{repo.id}/env")
    assert get_response.status_code == 200
    assert get_response.json()["data"] == []
    assert environment_variable_service.list_repository_defaults(app_db_session, repo.id) == []


# 3. PUT rejects invalid key format.
def test_put_rejects_invalid_key_format(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6003)
    repo = make_repository(app_db_session, user, github_repo_id=7003)
    _login(authed_client, app_db_session, user)

    response = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "not valid!", "value": "x"}]},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_ENV_VAR"
    assert "not valid!" in body["error"]["details"]["invalid_keys"]


# 4. PUT rejects duplicate keys within one payload.
def test_put_rejects_duplicate_keys_in_payload(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6004)
    repo = make_repository(app_db_session, user, github_repo_id=7004)
    _login(authed_client, app_db_session, user)

    response = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}, {"key": "PORT", "value": "4000"}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ENV_VAR"


# 5a. GET/PUT reject a repository not owned by the caller.
def test_get_and_put_reject_repository_not_owned(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=6005)
    owner_repo = make_repository(app_db_session, owner, github_repo_id=7005)

    attacker = make_user(app_db_session, github_id=6006)
    _login(authed_client, app_db_session, attacker)

    get_response = authed_client.get(f"/api/v1/repositories/{owner_repo.id}/env")
    assert get_response.status_code == 404
    assert get_response.json()["error"]["code"] == "REPO_NOT_FOUND"

    put_response = authed_client.put(
        f"/api/v1/repositories/{owner_repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}]},
    )
    assert put_response.status_code == 404
    assert put_response.json()["error"]["code"] == "REPO_NOT_FOUND"


# 5b. GET/PUT require authentication.
def test_get_and_put_require_authentication(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6007)
    repo = make_repository(app_db_session, user, github_repo_id=7006)
    # deliberately not logging in

    get_response = authed_client.get(f"/api/v1/repositories/{repo.id}/env")
    assert get_response.status_code == 401
    assert get_response.json()["error"]["code"] == "NOT_AUTHENTICATED"

    put_response = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env", json={"env_vars": []}
    )
    assert put_response.status_code == 401
    assert put_response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 9. Resolution reflects the CURRENT repository default at trigger time
#    (defaults are not snapshotted — only overrides are), for a
#    deployment created with no override for that key.
def test_engine_payload_reflects_default_changed_after_deployment_creation(
    authed_client, app_db_session, monkeypatch
):
    user = make_user(app_db_session, github_id=6008)
    repo = make_repository(app_db_session, user, github_repo_id=7007)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}]},
    )

    create_response = authed_client.post(
        "/api/v1/deployments", json={"repository_id": str(repo.id)}
    )
    assert create_response.status_code == 201

    # Change the default AFTER the deployment row exists, before the
    # (still-pending, mocked) trigger call happens.
    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "9090"}]},
    )

    deployment_id = create_response.json()["data"]["id"]
    from uuid import UUID

    from app.models.deployment import Deployment

    deployment = app_db_session.execute(
        select(Deployment).where(Deployment.id == UUID(deployment_id))
    ).scalar_one()

    resolved = environment_variable_service.resolve_effective_env_vars(app_db_session, deployment)
    effective = {item["key"]: item["value"] for item in resolved}
    assert effective["PORT"] == "9090"  # reflects the CURRENT default, not the value at creation time


# 11a. Plaintext values are never stored in value_encrypted.
def test_plaintext_never_stored(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6009)
    repo = make_repository(app_db_session, user, github_repo_id=7008)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "SECRET_TOKEN", "value": "super-secret-plaintext"}]},
    )

    row = app_db_session.execute(
        select(EnvironmentVariable).where(
            EnvironmentVariable.repository_id == repo.id,
            EnvironmentVariable.key == "SECRET_TOKEN",
        )
    ).scalar_one()

    assert row.value_encrypted != "super-secret-plaintext"
    assert "super-secret-plaintext" not in row.value_encrypted
    assert decrypt_value(row.value_encrypted) == "super-secret-plaintext"


# 11b. Plaintext values never appear in a GET/PUT response body.
def test_plaintext_never_appears_in_response_body(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6010)
    repo = make_repository(app_db_session, user, github_repo_id=7009)
    _login(authed_client, app_db_session, user)

    put_response = authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "API_KEY", "value": "sk_live_should_not_leak"}]},
    )
    assert "sk_live_should_not_leak" not in put_response.text

    get_response = authed_client.get(f"/api/v1/repositories/{repo.id}/env")
    assert "sk_live_should_not_leak" not in get_response.text


# 12. is_sensitive is computed correctly for KEY/SECRET/TOKEN/PASSWORD vs not.
def test_is_sensitive_computed_correctly(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=6011)
    repo = make_repository(app_db_session, user, github_repo_id=7010)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={
            "env_vars": [
                {"key": "API_KEY", "value": "a"},
                {"key": "DB_PASSWORD", "value": "b"},
                {"key": "AUTH_TOKEN", "value": "c"},
                {"key": "SOME_SECRET", "value": "d"},
                {"key": "PORT", "value": "e"},
                {"key": "NODE_ENV", "value": "f"},
            ]
        },
    )

    rows = environment_variable_service.list_repository_defaults(app_db_session, repo.id)
    sensitivity = {row.key: row.is_sensitive for row in rows}

    assert sensitivity["API_KEY"] is True
    assert sensitivity["DB_PASSWORD"] is True
    assert sensitivity["AUTH_TOKEN"] is True
    assert sensitivity["SOME_SECRET"] is True
    assert sensitivity["PORT"] is False
    assert sensitivity["NODE_ENV"] is False


# Unit-level: validate_env_vars raises with every offending key, combined.
def test_validate_env_vars_reports_all_offending_keys():
    try:
        environment_variable_service.validate_env_vars(
            [
                {"key": "valid_but_lowercase", "value": "x"},
                {"key": "PORT", "value": "1"},
                {"key": "PORT", "value": "2"},  # duplicate of a valid key
            ]
        )
        raise AssertionError("expected InvalidEnvVarError")
    except InvalidEnvVarError as exc:
        assert "valid_but_lowercase" in exc.invalid_keys
        assert "PORT" in exc.invalid_keys  # flagged for being duplicated, even though well-formed


# Unit-level: a well-formed, non-duplicated payload passes validation silently.
def test_validate_env_vars_accepts_well_formed_payload():
    environment_variable_service.validate_env_vars(
        [{"key": "PORT", "value": "3000"}, {"key": "NODE_ENV", "value": "production"}]
    )  # no exception
