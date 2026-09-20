"""
Tests for GET /api/v1/deployments/{id}/metrics (§7.1) and
POST /internal/engine-callback/metrics (§9.4)
(app/api/v1/deployments.py, app/api/internal.py,
app/services/monitoring_service.py,
app/services/deployment_callback_service.py).

Reuses the SAVEPOINT-based app_db_session/authed_client/client fixtures
from tests/test_auth.py (imported, not duplicated), and the
_wired_client/TEST_TOKEN pattern already established in
tests/test_internal_callbacks.py (re-declared locally, not imported,
since importing across sibling test modules for a two-line helper isn't
worth the coupling — same content, same reasoning). Neither
tests/conftest.py nor tests/test_auth.py is modified by this file.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.monitoring_metric import MonitoringMetric
from app.services import session_service
from tests.conftest import make_deployment, make_repository, make_user
from tests.test_auth import app_db_session, authed_client, client  # noqa: F401

TEST_TOKEN = "test-internal-token"


def _login(authed_client, app_db_session, user) -> str:
    raw_token, _session = session_service.create_session(app_db_session, user)
    authed_client.cookies.set(settings.session_cookie_name, raw_token)
    return raw_token


def _wired_client(client, app_db_session, monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", TEST_TOKEN)
    app.dependency_overrides[get_db] = lambda: app_db_session
    return client


# --- GET /api/v1/deployments/{id}/metrics ---


# 1. Auth required.
def test_get_metrics_requires_authentication(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8001)
    repo = make_repository(app_db_session, user, github_repo_id=9001)
    deployment = make_deployment(app_db_session, repo, status="running")

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/metrics")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# 2. Not-owned/missing deployment -> 404 DEPLOYMENT_NOT_FOUND.
def test_get_metrics_not_found_for_other_users_deployment(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=8002)
    owner_repo = make_repository(app_db_session, owner, github_repo_id=9002)
    deployment = make_deployment(app_db_session, owner_repo, status="running")

    attacker = make_user(app_db_session, github_id=8003)
    _login(authed_client, app_db_session, attacker)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/metrics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"


def test_get_metrics_not_found_for_nonexistent_id(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8004)
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{uuid.uuid4()}/metrics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"


# 3. Zero metrics ever -> 404 METRICS_NOT_AVAILABLE.
def test_get_metrics_returns_not_available_when_no_samples_exist(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8005)
    repo = make_repository(app_db_session, user, github_repo_id=9005)
    deployment = make_deployment(app_db_session, repo, status="pending")
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/metrics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "METRICS_NOT_AVAILABLE"


# 4. Correct latest/history/container_status shape.
def test_get_metrics_returns_latest_and_history(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8006)
    repo = make_repository(app_db_session, user, github_repo_id=9006)
    deployment = make_deployment(app_db_session, repo, status="running")

    base = datetime.now(timezone.utc)
    app_db_session.add_all(
        [
            MonitoringMetric(
                deployment_id=deployment.id,
                container_status="running",
                cpu_percent=10.1,
                memory_mb=172.0,
                timestamp=base,
            ),
            MonitoringMetric(
                deployment_id=deployment.id,
                container_status="running",
                cpu_percent=12.4,
                memory_mb=184.2,
                timestamp=base + timedelta(minutes=1),
            ),
        ]
    )
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/metrics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["deployment_id"] == str(deployment.id)
    assert data["container_status"] == "running"
    assert data["latest"]["cpu_percent"] == 12.4  # the more recent sample
    assert len(data["history"]) == 2
    assert data["history"][0]["cpu_percent"] == 10.1  # ascending chronological order
    assert data["history"][1]["cpu_percent"] == 12.4


# 5. `since` filters history but NOT latest.
def test_get_metrics_since_filters_history_not_latest(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8007)
    repo = make_repository(app_db_session, user, github_repo_id=9007)
    deployment = make_deployment(app_db_session, repo, status="running")

    base = datetime.now(timezone.utc)
    old_point = MonitoringMetric(
        deployment_id=deployment.id,
        container_status="running",
        cpu_percent=5.0,
        memory_mb=100.0,
        timestamp=base,
    )
    new_point = MonitoringMetric(
        deployment_id=deployment.id,
        container_status="running",
        cpu_percent=20.0,
        memory_mb=200.0,
        timestamp=base + timedelta(minutes=5),
    )
    app_db_session.add_all([old_point, new_point])
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    response = authed_client.get(
        f"/api/v1/deployments/{deployment.id}/metrics",
        params={"since": (base + timedelta(minutes=5)).isoformat()},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    # latest is unaffected by `since` — still the true most recent sample.
    assert data["latest"]["cpu_percent"] == 20.0
    # history only includes points at/after `since`.
    assert len(data["history"]) == 1
    assert data["history"][0]["cpu_percent"] == 20.0


# 6. `limit` bounds history.
def test_get_metrics_limit_bounds_history(authed_client, app_db_session):
    user = make_user(app_db_session, github_id=8008)
    repo = make_repository(app_db_session, user, github_repo_id=9008)
    deployment = make_deployment(app_db_session, repo, status="running")

    base = datetime.now(timezone.utc)
    app_db_session.add_all(
        [
            MonitoringMetric(
                deployment_id=deployment.id,
                container_status="running",
                cpu_percent=float(i),
                memory_mb=100.0,
                timestamp=base + timedelta(minutes=i),
            )
            for i in range(5)
        ]
    )
    app_db_session.flush()
    _login(authed_client, app_db_session, user)

    response = authed_client.get(
        f"/api/v1/deployments/{deployment.id}/metrics", params={"limit": 2}
    )

    assert response.status_code == 200
    assert len(response.json()["data"]["history"]) == 2


# --- POST /internal/engine-callback/metrics ---


# 7. Successful callback persists a row and returns the exact shape.
def test_metrics_callback_persists_sample(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    user = make_user(app_db_session, github_id=8009)
    repo = make_repository(app_db_session, user, github_repo_id=9009)
    deployment = make_deployment(app_db_session, repo, status="running")

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(deployment.id),
            "container_status": "running",
            "cpu_percent": 12.4,
            "memory_mb": 184.2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}

    stored = (
        app_db_session.query(MonitoringMetric)
        .filter(MonitoringMetric.deployment_id == deployment.id)
        .all()
    )
    assert len(stored) == 1
    assert stored[0].container_status == "running"
    assert float(stored[0].cpu_percent) == 12.4

    app.dependency_overrides.pop(get_db, None)


# 8. Missing/wrong token -> 401, checked before body validation.
def test_metrics_callback_missing_token_returns_401(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(uuid.uuid4()),
            "container_status": "not-a-real-status",  # deliberately also invalid
            "cpu_percent": 1.0,
            "memory_mb": 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Even with an invalid body, the missing token wins — proves the
    # same auth-before-body ordering already fixed for status/logs.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"

    app.dependency_overrides.pop(get_db, None)


def test_metrics_callback_wrong_token_returns_401(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    user = make_user(app_db_session, github_id=8010)
    repo = make_repository(app_db_session, user, github_repo_id=9010)
    deployment = make_deployment(app_db_session, repo, status="running")

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(deployment.id),
            "container_status": "running",
            "cpu_percent": 1.0,
            "memory_mb": 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"

    app.dependency_overrides.pop(get_db, None)


# 9. Unconfigured token -> 500 CONFIGURATION_ERROR.
def test_metrics_callback_returns_configuration_error_when_token_unset(
    client, app_db_session, monkeypatch
):
    monkeypatch.setattr(settings, "internal_api_token", None)
    app.dependency_overrides[get_db] = lambda: app_db_session

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(uuid.uuid4()),
            "container_status": "running",
            "cpu_percent": 1.0,
            "memory_mb": 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": "anything"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"

    app.dependency_overrides.pop(get_db, None)


# 10. Invalid container_status -> 422 VALIDATION_ERROR (JSON-safe).
def test_metrics_callback_rejects_unknown_container_status(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    user = make_user(app_db_session, github_id=8011)
    repo = make_repository(app_db_session, user, github_repo_id=9011)
    deployment = make_deployment(app_db_session, repo, status="running")

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(deployment.id),
            "container_status": "not-a-real-status",
            "cpu_percent": 1.0,
            "memory_mb": 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    app.dependency_overrides.pop(get_db, None)


# 11. Unknown deployment_id -> 404 DEPLOYMENT_NOT_FOUND.
def test_metrics_callback_unknown_deployment_returns_404(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)

    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(uuid.uuid4()),
            "container_status": "running",
            "cpu_percent": 1.0,
            "memory_mb": 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"

    app.dependency_overrides.pop(get_db, None)


# 12. Cross-user isolation on the GET side -> 404, not 403.
def test_get_metrics_isolation_returns_404_not_403(authed_client, app_db_session):
    owner = make_user(app_db_session, github_id=8012)
    owner_repo = make_repository(app_db_session, owner, github_repo_id=9012)
    deployment = make_deployment(app_db_session, owner_repo, status="running")
    app_db_session.add(
        MonitoringMetric(
            deployment_id=deployment.id,
            container_status="running",
            cpu_percent=1.0,
            memory_mb=1.0,
            timestamp=datetime.now(timezone.utc),
        )
    )
    app_db_session.flush()

    attacker = make_user(app_db_session, github_id=8013)
    _login(authed_client, app_db_session, attacker)

    response = authed_client.get(f"/api/v1/deployments/{deployment.id}/metrics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"
