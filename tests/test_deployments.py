"""
Tests for POST/GET /api/v1/deployments, GET /api/v1/deployments/{id},
and GET /api/v1/deployments/{id}/logs
(app/api/v1/deployments.py, app/services/deployment_service.py).

Stage 6 additions at the end of this file cover the engine-trigger side
effect of POST /deployments: app.services.deployment_engine_client.
trigger_deployment is mocked via monkeypatch — no real network call is
ever made, same pattern used throughout this project. BackgroundTasks
scheduled during the request complete within the TestClient request
cycle, so the deployment row's post-trigger state can be asserted
immediately after the response returns.

Reuses the SAVEPOINT-based app_db_session/authed_client/client fixtures
from tests/test_auth.py (imported, not duplicated) since
deployment_service calls db.commit() for real — same reasoning as
tests/test_repositories.py. Neither tests/conftest.py nor
tests/test_auth.py is modified by this file.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.deployment import Deployment
from app.models.deployment_log import DeploymentLog
from app.models.environment_variable import EnvironmentVariable
from app.services import deployment_engine_client, deployment_service, session_service
from tests.conftest import make_deployment, make_repository, make_user
from tests.test_auth import app_db_session, authed_client, client  # noqa: F401


def _login(authed_client, app_db_session, user) -> str:
    raw_token, _session = session_service.create_session(app_db_session, user)
    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    return raw_token


# 1. Creating a deployment succeeds.
def test_create_deployment_succeeds(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2001)
    repo = make_repository(app_db_session, user, github_repo_id=3001)
    _login(authed_client, app_db_session, user)

    response = authed_client.post(
        "/api/v1/deployments", json={"repository_id": str(repo.id), "branch": "main"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["data"]["repository_id"] == str(repo.id)
    assert body["data"]["branch"] == "main"
    assert body["data"]["status"] == "pending"
    assert body["data"]["live_url"] is None


# 2. Omitted branch defaults to the repository's default_branch.
def test_create_deployment_uses_default_branch_when_omitted(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2002)
    repo = make_repository(app_db_session, user, github_repo_id=3002)
    repo.default_branch = "develop"
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 201
    assert response.json()["data"]["branch"] == "develop"


# 3. Repository not owned by the caller is rejected.
def test_create_deployment_rejects_repository_not_owned(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=2003)
    other_repo = make_repository(app_db_session, owner, github_repo_id=3003)

    attacker = make_user(app_db_session, github_id=2004)
    _login(authed_client, app_db_session, attacker)

    response = authed_client.post(
        "/api/v1/deployments", json={"repository_id": str(other_repo.id)}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPO_NOT_FOUND"


# 4. Nonexistent repository is rejected.
def test_create_deployment_rejects_nonexistent_repository(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2005)
    _login(authed_client, app_db_session, user)

    response = authed_client.post(
        "/api/v1/deployments", json={"repository_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPO_NOT_FOUND"


# 5. Same repository cannot have two active deployments — 409.
def test_create_deployment_conflict_when_already_active(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2006)
    repo = make_repository(app_db_session, user, github_repo_id=3006)
    active = make_deployment(app_db_session, repo, status="building")
    _login(authed_client, app_db_session, user)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "DEPLOYMENT_IN_PROGRESS"
    # API_CONTRACT.md §4.1: details.existing_deployment_id is the active one.
    assert error["details"] == {"existing_deployment_id": str(active.id)}


# 6. A running deployment does NOT block a new pending deployment.
def test_create_deployment_allowed_when_existing_is_running(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2007)
    repo = make_repository(app_db_session, user, github_repo_id=3007)
    make_deployment(app_db_session, repo, status="running")
    _login(authed_client, app_db_session, user)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 201
    assert response.json()["data"]["status"] == "pending"


# 7. Unauthenticated create is rejected.
def test_create_deployment_requires_authentication(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2008)
    repo = make_repository(app_db_session, user, github_repo_id=3008)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 8. Listing only returns the current user's own deployments.
def test_list_deployments_returns_only_current_users_deployments(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2009)
    repo = make_repository(app_db_session, user, github_repo_id=3009)
    make_deployment(app_db_session, repo, status="running")

    other_user = make_user(app_db_session, github_id=2010)
    other_repo = make_repository(app_db_session, other_user, github_repo_id=3010)
    make_deployment(app_db_session, other_repo, status="running")

    _login(authed_client, app_db_session, user)

    response = authed_client.get("/api/v1/deployments")

    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["repository_id"] == str(repo.id)


# 9. Filtering by repository_id works.
def test_list_deployments_filters_by_repository_id(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2011)
    repo_a = make_repository(app_db_session, user, github_repo_id=3011)
    repo_b = make_repository(app_db_session, user, github_repo_id=3012)
    make_deployment(app_db_session, repo_a, status="running")
    make_deployment(app_db_session, repo_b, status="running")
    _login(authed_client, app_db_session, user)

    response = authed_client.get(
        "/api/v1/deployments", params={"repository_id": str(repo_a.id)}
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["repository_id"] == str(repo_a.id)


# 10. Filtering by status works.
def test_list_deployments_filters_by_status(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2013)
    repo = make_repository(app_db_session, user, github_repo_id=3013)
    make_deployment(app_db_session, repo, status="failed")

    repo2 = make_repository(app_db_session, user, github_repo_id=3014)
    make_deployment(app_db_session, repo2, status="running")

    _login(authed_client, app_db_session, user)

    response = authed_client.get("/api/v1/deployments", params={"status": "failed"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "failed"


# 11. Pagination works.
def test_list_deployments_pagination(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2015)
    repos = [make_repository(app_db_session, user, github_repo_id=3015 + i) for i in range(3)]
    for repo in repos:
        make_deployment(app_db_session, repo, status="running")
    _login(authed_client, app_db_session, user)

    page1 = authed_client.get(
        "/api/v1/deployments", params={"page": 1, "per_page": 2}
    ).json()["data"]
    page2 = authed_client.get(
        "/api/v1/deployments", params={"page": 2, "per_page": 2}
    ).json()["data"]

    assert len(page1) == 2
    assert len(page2) == 1
    ids_page1 = {d["id"] for d in page1}
    ids_page2 = {d["id"] for d in page2}
    assert ids_page1.isdisjoint(ids_page2)
    assert len(ids_page1 | ids_page2) == 3


# 12. Detail lookup succeeds.
def test_get_deployment_detail_succeeds(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2016)
    repo = make_repository(app_db_session, user, github_repo_id=3016)
    deployment = make_deployment(app_db_session, repo, status="running")
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["id"] == str(deployment.id)
    assert body["data"]["status"] == "running"


# 13. Another user's deployment returns 404, not 403 (no ownership leak).
def test_get_deployment_not_found_for_other_users_deployment(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=2017)
    owner_repo = make_repository(app_db_session, owner, github_repo_id=3017)
    deployment = make_deployment(app_db_session, owner_repo, status="running")

    attacker = make_user(app_db_session, github_id=2018)
    _login(authed_client, app_db_session, attacker)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"


# 14. Nonexistent deployment id returns 404.
def test_get_deployment_not_found_for_nonexistent_id(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2019)
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"


# 15. Unauthenticated detail lookup is rejected.
def test_get_deployment_requires_authentication(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2020)
    repo = make_repository(app_db_session, user, github_repo_id=3020)
    deployment = make_deployment(app_db_session, repo, status="running")

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 16. No logs yet -> empty list (read-only, no producer exists yet).
def test_get_logs_returns_empty_list_when_no_logs_exist(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2021)
    repo = make_repository(app_db_session, user, github_repo_id=3021)
    deployment = make_deployment(app_db_session, repo, status="building")
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs")

    assert response.status_code == 200
    assert response.json()["data"] == []


# 17. Existing logs are returned in timestamp order.
def test_get_logs_returns_existing_logs_in_order(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2022)
    repo = make_repository(app_db_session, user, github_repo_id=3022)
    deployment = make_deployment(app_db_session, repo, status="building")

    base = datetime.now(timezone.utc)
    app_db_session.add_all(
        [
            DeploymentLog(
                deployment_id=deployment.id,
                timestamp=base + timedelta(seconds=2),
                level="info",
                message="second",
            ),
            DeploymentLog(
                deployment_id=deployment.id,
                timestamp=base,
                level="info",
                message="first",
            ),
        ]
    )
    app_db_session.flush()

    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs")

    assert response.status_code == 200
    messages = [log["message"] for log in response.json()["data"]]
    assert messages == ["first", "second"]


# 18. Logs for another user's deployment return 404.
def test_get_logs_not_found_for_other_users_deployment(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=2023)
    owner_repo = make_repository(app_db_session, owner, github_repo_id=3023)
    deployment = make_deployment(app_db_session, owner_repo, status="building")

    attacker = make_user(app_db_session, github_id=2024)
    _login(authed_client, app_db_session, attacker)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"


# 19. Unauthenticated logs request is rejected.
def test_get_logs_requires_authentication(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2025)
    repo = make_repository(app_db_session, user, github_repo_id=3025)
    deployment = make_deployment(app_db_session, repo, status="building")

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# --- Stage 6: engine-trigger side effect of POST /deployments ---


# 20. Creating a deployment triggers the engine client with the correct payload.
def test_create_deployment_triggers_engine_with_correct_payload(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=2026)
    repo = make_repository(app_db_session, user, github_repo_id=3026)
    repo.clone_url = "https://github.com/test-user/sample-repo.git"
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    monkeypatch.setattr(settings, "engine_base_url", "http://engine.internal:9000")
    monkeypatch.setattr(settings, "internal_api_token", "shared-secret")

    seen_calls = []

    async def _fake_trigger(deployment_id, *, clone_url, branch, env_vars):
        seen_calls.append((deployment_id, clone_url, branch, env_vars))
        return "job_123"

    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", _fake_trigger)
    # run_engine_trigger now always opens a DB session (to resolve env
    # vars, Stage 8) even on the success path, not just on failure —
    # redirect it to the isolated test session, same reasoning as the
    # failure-path tests below.
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)

    response = authed_client.post(
        "/api/v1/deployments", json={"repository_id": str(repo.id), "branch": "main"}
    )

    assert response.status_code == 201
    deployment_id = uuid.UUID(response.json()["data"]["id"])

    assert len(seen_calls) == 1
    called_id, called_clone_url, called_branch, called_env_vars = seen_calls[0]
    assert called_id == deployment_id
    assert called_clone_url == "https://github.com/test-user/sample-repo.git"
    assert called_branch == "main"
    assert called_env_vars == []  # no repo defaults or deployment overrides set up in this test


# 21. Engine trigger failure marks the deployment "failed" with a generic error message.
def test_create_deployment_marks_failed_when_engine_trigger_fails(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=2027)
    repo = make_repository(app_db_session, user, github_repo_id=3027)
    _login(authed_client, app_db_session, user)

    monkeypatch.setattr(settings, "engine_base_url", "http://engine.internal:9000")
    monkeypatch.setattr(settings, "internal_api_token", "shared-secret")

    async def _fake_trigger(deployment_id, *, clone_url, branch, env_vars):
        raise deployment_engine_client.EngineUnavailableError("simulated engine outage")

    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", _fake_trigger)

    # The failure path in run_engine_trigger opens its OWN DB session via
    # the module-level SessionLocal (the real app engine — correct for
    # production, since a background task has no request-scoped session
    # to reuse). That's a different connection from app_db_session here,
    # so it's redirected to the same isolated test session, the same way
    # get_db() is overridden for the request itself — otherwise this
    # test would be asserting against a connection its own rollback
    # never touches.
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    # The create request itself still succeeds — only the background
    # trigger fails, which is reflected in the row afterward, not in
    # this response.
    assert response.status_code == 201
    deployment_id = uuid.UUID(response.json()["data"]["id"])

    updated = app_db_session.execute(
        select(Deployment).where(Deployment.id == deployment_id)
    ).scalar_one()
    assert updated.status == "failed"
    assert updated.error_message == "Deployment engine unreachable"


# 22. Engine trigger success leaves the deployment's status unchanged ("pending").
def test_create_deployment_stays_pending_when_engine_trigger_succeeds(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=2028)
    repo = make_repository(app_db_session, user, github_repo_id=3028)
    _login(authed_client, app_db_session, user)

    monkeypatch.setattr(settings, "engine_base_url", "http://engine.internal:9000")
    monkeypatch.setattr(settings, "internal_api_token", "shared-secret")

    async def _fake_trigger(deployment_id, *, clone_url, branch, env_vars):
        return "job_456"

    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", _fake_trigger)
    # run_engine_trigger now always opens a DB session (to resolve env
    # vars, Stage 8) even on the success path — redirect it, same
    # reasoning as test 20/21.
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 201
    deployment_id = uuid.UUID(response.json()["data"]["id"])

    updated = app_db_session.execute(
        select(Deployment).where(Deployment.id == deployment_id)
    ).scalar_one()
    assert updated.status == "pending"
    assert updated.error_message is None


# --- Stage 7: ordering stability, since-boundary, log id, failure-log consistency ---


# 23. Deployment listing order is deterministic when created_at ties.
def test_list_deployments_ordering_stable_with_equal_created_at(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2029)
    repo_a = make_repository(app_db_session, user, github_repo_id=3029)
    repo_b = make_repository(app_db_session, user, github_repo_id=3030)

    tied_time = datetime.now(timezone.utc)
    deployment_a = make_deployment(app_db_session, repo_a, status="running")
    deployment_b = make_deployment(app_db_session, repo_b, status="running")
    # Force an identical created_at so the secondary sort key (id) is
    # the only thing that can make ordering deterministic.
    deployment_a.created_at = tied_time
    deployment_b.created_at = tied_time
    app_db_session.flush()

    _login(authed_client, app_db_session, user)

    first_response = authed_client.get("/api/v1/deployments").json()["data"]
    second_response = authed_client.get("/api/v1/deployments").json()["data"]

    first_ids = [d["id"] for d in first_response]
    second_ids = [d["id"] for d in second_response]
    assert first_ids == second_ids  # same order on repeated identical requests

    # And that order matches the expected tie-break: id ascending among
    # equal created_at values.
    tied_ids_in_response = [d["id"] for d in first_response if d["id"] in {str(deployment_a.id), str(deployment_b.id)}]
    expected_order = sorted([str(deployment_a.id), str(deployment_b.id)])
    assert tied_ids_in_response == expected_order


# 24. Log listing order is deterministic when timestamp ties.
def test_get_logs_ordering_stable_with_equal_timestamp(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2030)
    repo = make_repository(app_db_session, user, github_repo_id=3031)
    deployment = make_deployment(app_db_session, repo, status="building")

    tied_time = datetime.now(timezone.utc)
    log_a = DeploymentLog(deployment_id=deployment.id, timestamp=tied_time, level="info", message="a")
    log_b = DeploymentLog(deployment_id=deployment.id, timestamp=tied_time, level="info", message="b")
    app_db_session.add_all([log_a, log_b])
    app_db_session.flush()

    _login(authed_client, app_db_session, user)

    first = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs").json()["data"]
    second = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs").json()["data"]

    assert [entry["id"] for entry in first] == [entry["id"] for entry in second]
    assert [entry["id"] for entry in first] == sorted(entry["id"] for entry in first)


# 25. `since` is inclusive: a log line tied with the boundary timestamp is not dropped.
def test_get_logs_since_inclusive_does_not_drop_tied_timestamp_line(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2031)
    repo = make_repository(app_db_session, user, github_repo_id=3032)
    deployment = make_deployment(app_db_session, repo, status="building")

    boundary_time = datetime.now(timezone.utc)
    seen_line = DeploymentLog(
        deployment_id=deployment.id, timestamp=boundary_time, level="info", message="seen"
    )
    tied_line = DeploymentLog(
        deployment_id=deployment.id, timestamp=boundary_time, level="info", message="tied"
    )
    later_line = DeploymentLog(
        deployment_id=deployment.id,
        timestamp=boundary_time + timedelta(seconds=1),
        level="info",
        message="later",
    )
    app_db_session.add_all([seen_line, tied_line, later_line])
    app_db_session.flush()

    _login(authed_client, app_db_session, user)

    response = authed_client.get(
        f"/api/v1/deployments/{deployment.id}/logs",
        params={"since": boundary_time.isoformat()},
    )

    assert response.status_code == 200
    messages = {entry["message"] for entry in response.json()["data"]}
    # Both lines tied at the boundary are present (not silently dropped),
    # plus the later one.
    assert messages == {"seen", "tied", "later"}


# 26. Log response items include the new `id` field.
def test_get_logs_response_includes_id_field(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2032)
    repo = make_repository(app_db_session, user, github_repo_id=3033)
    deployment = make_deployment(app_db_session, repo, status="building")

    log = DeploymentLog(
        deployment_id=deployment.id,
        timestamp=datetime.now(timezone.utc),
        level="info",
        message="hello",
    )
    app_db_session.add(log)
    app_db_session.flush()

    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/logs")

    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["id"] == log.id
    assert isinstance(entry["id"], int)


# 27. Engine trigger failure creates a visible error log line matching error_message.
def test_engine_trigger_failure_appends_error_log_line(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=2033)
    repo = make_repository(app_db_session, user, github_repo_id=3034)
    _login(authed_client, app_db_session, user)

    monkeypatch.setattr(settings, "engine_base_url", "http://engine.internal:9000")
    monkeypatch.setattr(settings, "internal_api_token", "shared-secret")

    async def _fake_trigger(deployment_id, *, clone_url, branch, env_vars):
        raise deployment_engine_client.EngineUnavailableError("simulated engine outage")

    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", _fake_trigger)
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 201
    deployment_id = uuid.UUID(response.json()["data"]["id"])

    updated = app_db_session.execute(
        select(Deployment).where(Deployment.id == deployment_id)
    ).scalar_one()
    assert updated.status == "failed"
    assert updated.error_message == "Deployment engine unreachable"

    logs = app_db_session.execute(
        select(DeploymentLog).where(DeploymentLog.deployment_id == deployment_id)
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].level == "error"
    assert logs[0].message == updated.error_message == "Deployment engine unreachable"


# 28. POST /deployments with env_vars snapshots override rows created
#     atomically with the deployment (Stage 8 correction: deployment +
#     override snapshot must both persist or neither does).
def test_create_deployment_with_env_vars_creates_override_rows_atomically(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2034)
    repo = make_repository(app_db_session, user, github_repo_id=3035)
    _login(authed_client, app_db_session, user)

    response = authed_client.post(
        "/api/v1/deployments",
        json={
            "repository_id": str(repo.id),
            "env_vars": [
                {"key": "PORT", "value": "3000"},
                {"key": "API_KEY", "value": "sk_live_abc123"},
            ],
        },
    )

    assert response.status_code == 201
    deployment_id = uuid.UUID(response.json()["data"]["id"])

    override_rows = app_db_session.execute(
        select(EnvironmentVariable).where(EnvironmentVariable.deployment_id == deployment_id)
    ).scalars().all()
    assert {row.key for row in override_rows} == {"PORT", "API_KEY"}
    # Never stored as plaintext.
    for row in override_rows:
        assert row.value_encrypted not in ("3000", "sk_live_abc123")


# 29. Invalid env_vars in POST /deployments are rejected before any write
#     — no deployment row and no override rows are created (atomicity
#     from the other direction: a validation failure must leave nothing
#     behind).
def test_create_deployment_rejects_invalid_env_vars_before_any_write(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2035)
    repo = make_repository(app_db_session, user, github_repo_id=3036)
    _login(authed_client, app_db_session, user)

    response = authed_client.post(
        "/api/v1/deployments",
        json={
            "repository_id": str(repo.id),
            "env_vars": [{"key": "not-valid-key", "value": "x"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ENV_VAR"

    remaining_deployments = app_db_session.execute(
        select(Deployment).where(Deployment.repository_id == repo.id)
    ).scalars().all()
    assert remaining_deployments == []


# 30. POST /deployments env_vars rejects duplicate keys within one payload.
def test_create_deployment_rejects_duplicate_env_var_keys(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2036)
    repo = make_repository(app_db_session, user, github_repo_id=3037)
    _login(authed_client, app_db_session, user)

    response = authed_client.post(
        "/api/v1/deployments",
        json={
            "repository_id": str(repo.id),
            "env_vars": [
                {"key": "PORT", "value": "3000"},
                {"key": "PORT", "value": "4000"},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ENV_VAR"


# 31. Effective resolution: a deployment override wins over the
#     repository default for the same key, and the engine payload
#     reflects that.
def test_engine_payload_uses_override_over_default(authed_client, app_db_session, monkeypatch):
    user = make_user(app_db_session, github_id=2037)
    repo = make_repository(app_db_session, user, github_repo_id=3038)
    _login(authed_client, app_db_session, user)

    authed_client.put(
        f"/api/v1/repositories/{repo.id}/env",
        json={"env_vars": [{"key": "PORT", "value": "3000"}, {"key": "NODE_ENV", "value": "production"}]},
    )

    monkeypatch.setattr(settings, "engine_base_url", "http://engine.internal:9000")
    monkeypatch.setattr(settings, "internal_api_token", "shared-secret")

    seen = []

    async def _fake_trigger(deployment_id, *, clone_url, branch, env_vars):
        seen.append(env_vars)
        return "job_789"

    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", _fake_trigger)
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)

    response = authed_client.post(
        "/api/v1/deployments",
        json={"repository_id": str(repo.id), "env_vars": [{"key": "PORT", "value": "8080"}]},
    )

    assert response.status_code == 201
    assert len(seen) == 1
    effective = {item["key"]: item["value"] for item in seen[0]}
    assert effective["PORT"] == "8080"  # override wins
    assert effective["NODE_ENV"] == "production"  # default, no override provided


# --- Contract-compliance fixes: 409 details, meta, repository_name ---


# 32. The 409 points at the ACTIVE deployment, not an older running/stopped one.
def test_create_deployment_conflict_details_reference_the_active_deployment(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2038)
    repo = make_repository(app_db_session, user, github_repo_id=3039)
    make_deployment(app_db_session, repo, status="running")
    make_deployment(app_db_session, repo, status="stopped")
    active = make_deployment(app_db_session, repo, status="starting")
    _login(authed_client, app_db_session, user)

    response = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "DEPLOYMENT_IN_PROGRESS"
    assert body["error"]["details"] == {"existing_deployment_id": str(active.id)}


# 33. The lost-race lookup helper finds the active deployment (or None).
def test_find_active_deployment_id_returns_active_deployment_or_none(app_db_session):
    user = make_user(app_db_session, github_id=2039)
    repo = make_repository(app_db_session, user, github_repo_id=3040)
    make_deployment(app_db_session, repo, status="running")

    assert deployment_service._find_active_deployment_id(app_db_session, repo.id) is None

    active = make_deployment(app_db_session, repo, status="pending")

    assert deployment_service._find_active_deployment_id(app_db_session, repo.id) == active.id


# 34. GET /deployments returns meta with the default page/per_page.
def test_list_deployments_returns_meta_with_defaults(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2040)
    repo = make_repository(app_db_session, user, github_repo_id=3041)
    make_deployment(app_db_session, repo, status="running")
    _login(authed_client, app_db_session, user)

    response = authed_client.get("/api/v1/deployments")

    assert response.status_code == 200
    body = response.json()
    assert list(body.keys()) == ["data", "meta"]
    assert body["meta"] == {"page": 1, "per_page": 20, "has_next": False}


# 35. meta.has_next is exact: true only when another page really exists.
def test_list_deployments_meta_has_next_is_exact(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2041)
    repos = [make_repository(app_db_session, user, github_repo_id=3042 + i) for i in range(3)]
    for repo in repos:
        make_deployment(app_db_session, repo, status="running")
    _login(authed_client, app_db_session, user)

    page1 = authed_client.get("/api/v1/deployments", params={"page": 1, "per_page": 2}).json()
    page2 = authed_client.get("/api/v1/deployments", params={"page": 2, "per_page": 2}).json()
    exact_fit = authed_client.get("/api/v1/deployments", params={"page": 1, "per_page": 3}).json()

    assert len(page1["data"]) == 2
    assert page1["meta"] == {"page": 1, "per_page": 2, "has_next": True}
    assert len(page2["data"]) == 1
    assert page2["meta"] == {"page": 2, "per_page": 2, "has_next": False}
    # Exactly per_page items and nothing after them: no false "next page".
    assert len(exact_fit["data"]) == 3
    assert exact_fit["meta"] == {"page": 1, "per_page": 3, "has_next": False}


# 36. Deployment responses (create, detail, list) include repository_name.
def test_deployment_responses_include_repository_name(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2042)
    repo = make_repository(app_db_session, user, github_repo_id=3045)
    repo.name = "weather-app"
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    created = authed_client.post("/api/v1/deployments", json={"repository_id": str(repo.id)})
    assert created.status_code == 201
    assert created.json()["data"]["repository_name"] == "weather-app"
    deployment_id = created.json()["data"]["id"]

    detail = authed_client.get(f"/api/v1/deployments/{deployment_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["repository_name"] == "weather-app"

    listing = authed_client.get("/api/v1/deployments")
    assert listing.status_code == 200
    assert [item["repository_name"] for item in listing.json()["data"]] == ["weather-app"]


# 37. Each listed deployment carries ITS OWN repository's name.
def test_list_deployments_repository_name_matches_each_deployments_repository(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=2043)
    repo_a = make_repository(app_db_session, user, github_repo_id=3046)
    repo_a.name = "alpha"
    repo_b = make_repository(app_db_session, user, github_repo_id=3047)
    repo_b.name = "beta"
    app_db_session.flush()
    deployment_a = make_deployment(app_db_session, repo_a, status="running")
    deployment_b = make_deployment(app_db_session, repo_b, status="running")
    _login(authed_client, app_db_session, user)

    response = authed_client.get("/api/v1/deployments")

    assert response.status_code == 200
    names_by_id = {item["id"]: item["repository_name"] for item in response.json()["data"]}
    assert names_by_id == {str(deployment_a.id): "alpha", str(deployment_b.id): "beta"}
