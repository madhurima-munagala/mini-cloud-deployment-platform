"""
Tests for POST /internal/engine-callback/status and
POST /internal/engine-callback/logs (app/api/internal.py,
app/services/deployment_callback_service.py).

These endpoints use X-Internal-Token auth, not the session cookie — so
tests here don't log in a user; they just set/omit the header.

Reuses the SAVEPOINT-based app_db_session/client fixtures from
tests/test_auth.py (imported, not duplicated) since
deployment_callback_service calls db.commit() for real — same reasoning
as tests/test_deployments.py. Neither tests/conftest.py nor
tests/test_auth.py is modified by this file.
"""

import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.deployment_log import DeploymentLog
from tests.conftest import make_deployment, make_repository, make_user
from tests.test_auth import app_db_session, client  # noqa: F401

TEST_TOKEN = "test-internal-token"


def _wired_client(client, app_db_session, monkeypatch):
    """Overrides get_db() to the isolated test session and configures a
    known internal token, mirroring authed_client's pattern in the other
    Stage 4/5 test files but without any session cookie."""
    monkeypatch.setattr(settings, "internal_api_token", TEST_TOKEN)
    app.dependency_overrides[get_db] = lambda: app_db_session
    return client


def _deployment(app_db_session, *, status: str = "pending"):
    user = make_user(app_db_session, github_id=4001)
    repo = make_repository(app_db_session, user, github_repo_id=5001)
    return make_deployment(app_db_session, repo, status=status)


# --- status callback ---


def test_status_callback_updates_deployment(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session, status="pending")

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "running",
            "live_url": "http://ec2-1-2-3-4.compute.amazonaws.com:8421",
            "container_id": "c_abc123",
            "error_message": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}

    app_db_session.refresh(deployment)
    assert deployment.status == "running"
    assert deployment.live_url == "http://ec2-1-2-3-4.compute.amazonaws.com:8421"
    assert deployment.container_id == "c_abc123"

    app.dependency_overrides.pop(get_db, None)


def test_status_callback_unknown_deployment_returns_404(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(uuid.uuid4()),
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"

    app.dependency_overrides.pop(get_db, None)


def test_status_callback_missing_token_returns_401(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"

    app.dependency_overrides.pop(get_db, None)


def test_status_callback_wrong_token_returns_401(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"

    app.dependency_overrides.pop(get_db, None)


def test_status_callback_rejects_unknown_status_value(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "not-a-real-status",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    app.dependency_overrides.pop(get_db, None)


def test_status_callback_returns_configuration_error_when_token_unset(
    client, app_db_session, monkeypatch
):
    monkeypatch.setattr(settings, "internal_api_token", None)
    app.dependency_overrides[get_db] = lambda: app_db_session
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": "anything"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"

    app.dependency_overrides.pop(get_db, None)


# --- logs callback ---


def test_logs_callback_appends_multiple_lines(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session, status="building")

    response = client.post(
        "/internal/engine-callback/logs",
        json={
            "deployment_id": str(deployment.id),
            "logs": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "info",
                    "message": "Cloning repository...",
                },
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "info",
                    "message": "Building Docker image...",
                },
            ],
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True, "count": 2}

    stored = (
        app_db_session.query(DeploymentLog)
        .filter(DeploymentLog.deployment_id == deployment.id)
        .all()
    )
    assert len(stored) == 2
    assert {row.message for row in stored} == {"Cloning repository...", "Building Docker image..."}

    app.dependency_overrides.pop(get_db, None)


def test_logs_callback_unknown_deployment_returns_404(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    response = client.post(
        "/internal/engine-callback/logs",
        json={
            "deployment_id": str(uuid.uuid4()),
            "logs": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "info",
                    "message": "hello",
                }
            ],
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_FOUND"

    app.dependency_overrides.pop(get_db, None)


def test_logs_callback_missing_token_returns_401(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/logs",
        json={
            "deployment_id": str(deployment.id),
            "logs": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "info",
                    "message": "hello",
                }
            ],
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"

    app.dependency_overrides.pop(get_db, None)


def test_logs_callback_rejects_unknown_level_value(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session)

    response = client.post(
        "/internal/engine-callback/logs",
        json={
            "deployment_id": str(deployment.id),
            "logs": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "not-a-real-level",
                    "message": "hello",
                }
            ],
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    app.dependency_overrides.pop(get_db, None)


# --- Stage 7: status callback / log-line separation ---


# A normal engine status callback (even one reporting "failed") must NOT
# automatically create a log line — that's scoped specifically to the
# backend-detected trigger-failure path
# (deployment_callback_service.mark_deployment_failed), not to
# apply_status_update() in general. The engine is expected to send its
# own descriptive log lines separately, via .../engine-callback/logs.
def test_status_callback_does_not_duplicate_log_line(client, app_db_session, monkeypatch):
    _wired_client(client, app_db_session, monkeypatch)
    deployment = _deployment(app_db_session, status="building")

    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment.id),
            "status": "failed",
            "live_url": None,
            "container_id": None,
            "error_message": "build step exited with code 1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        headers={"X-Internal-Token": TEST_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}

    app_db_session.refresh(deployment)
    assert deployment.status == "failed"
    assert deployment.error_message == "build step exited with code 1"

    logs = (
        app_db_session.query(DeploymentLog)
        .filter(DeploymentLog.deployment_id == deployment.id)
        .all()
    )
    assert logs == []

    app.dependency_overrides.pop(get_db, None)
