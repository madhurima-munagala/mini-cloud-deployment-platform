"""
Applies engine-reported data to the existing `deployments`,
`deployment_logs`, and `monitoring_metrics` tables.

Used by three callers:
1. app/api/internal.py — the real engine callbacks
   (POST /internal/engine-callback/status, .../logs, .../metrics)
2. app/services/deployment_engine_client.py — the trigger-failure path,
   which reuses apply_status_update() (via mark_deployment_failed) so
   "the engine told us it failed" and "we couldn't reach the engine at
   all" are written through the exact same code path.
3. app/services/monitoring_service.py does NOT call into this module —
   it only reads. append_metric() below is the write side.

No new tables/columns — every field written here already exists on the
Deployment/DeploymentLog/MonitoringMetric models (app/models/deployment.py,
app/models/deployment_log.py, app/models/monitoring_metric.py).
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.deployment import Deployment
from app.models.deployment_log import DeploymentLog
from app.models.monitoring_metric import MonitoringMetric


def apply_status_update(
    db: DBSession,
    *,
    deployment_id: uuid.UUID,
    status: str,
    live_url: str | None,
    container_id: str | None,
    error_message: str | None,
) -> Deployment | None:
    """
    Updates the deployment row's status/live_url/container_id/error_message.
    Returns the updated Deployment, or None if deployment_id doesn't exist
    (caller maps that to 404 DEPLOYMENT_NOT_FOUND).
    """
    deployment = db.execute(
        select(Deployment).where(Deployment.id == deployment_id)
    ).scalar_one_or_none()
    if deployment is None:
        return None

    deployment.status = status
    deployment.live_url = live_url
    deployment.container_id = container_id
    deployment.error_message = error_message

    db.commit()
    db.refresh(deployment)
    return deployment


def mark_deployment_failed(db: DBSession, deployment_id: uuid.UUID, message: str) -> None:
    """
    Convenience wrapper used when the backend itself detects a failure
    (e.g. couldn't reach the engine at all) rather than receiving one via
    callback — writes through the same apply_status_update() path.
    Silently no-ops if the deployment no longer exists.

    Stage 7: also inserts a single error-level DeploymentLog row with
    the same message. This is the one case where no engine-reported log
    line could ever exist to explain the failure (the engine was never
    successfully reached), so without this a client polling only
    GET /deployments/{id}/logs would see nothing — the deployment's
    error_message would only be visible via the detail endpoint.
    apply_status_update() itself is intentionally left unchanged: a
    normal engine status callback (the engine reporting its own
    "failed") does NOT get an automatic log line here — the engine is
    expected to send its own descriptive log lines separately via
    POST /internal/engine-callback/logs.
    """
    deployment = apply_status_update(
        db,
        deployment_id=deployment_id,
        status="failed",
        live_url=None,
        container_id=None,
        error_message=message,
    )
    if deployment is None:
        return

    db.add(
        DeploymentLog(
            deployment_id=deployment_id,
            timestamp=datetime.now(timezone.utc),
            level="error",
            message=message,
        )
    )
    db.commit()


def append_logs(
    db: DBSession, *, deployment_id: uuid.UUID, logs: list[dict]
) -> int | None:
    """
    Bulk-inserts log lines for a deployment in one commit.
    Returns the number of lines inserted, or None if deployment_id
    doesn't exist (caller maps that to 404 DEPLOYMENT_NOT_FOUND).
    `logs` items: {"timestamp": datetime, "level": str, "message": str}.
    """
    deployment_exists = db.execute(
        select(Deployment.id).where(Deployment.id == deployment_id)
    ).scalar_one_or_none()
    if deployment_exists is None:
        return None

    rows = [
        DeploymentLog(
            deployment_id=deployment_id,
            timestamp=entry["timestamp"],
            level=entry["level"],
            message=entry["message"],
        )
        for entry in logs
    ]
    db.add_all(rows)
    db.commit()

    return len(rows)


def append_metric(
    db: DBSession,
    *,
    deployment_id: uuid.UUID,
    container_status: str,
    cpu_percent: float,
    memory_mb: float,
    timestamp: datetime,
) -> bool:
    """
    Inserts one monitoring sample (§9.4 — one point per call, unlike
    append_logs: the contract's metrics callback example is not batched,
    no "metrics": [...] array). Returns False if deployment_id doesn't
    exist (caller maps that to 404 DEPLOYMENT_NOT_FOUND), True otherwise.

    cpu_percent/memory_mb arrive as plain floats (from the parsed JSON
    body) and are converted via Decimal(str(...)) rather than
    Decimal(float) directly, to avoid binary-float representation drift
    landing in the NUMERIC column.
    """
    deployment_exists = db.execute(
        select(Deployment.id).where(Deployment.id == deployment_id)
    ).scalar_one_or_none()
    if deployment_exists is None:
        return False

    db.add(
        MonitoringMetric(
            deployment_id=deployment_id,
            container_status=container_status,
            cpu_percent=Decimal(str(cpu_percent)),
            memory_mb=Decimal(str(memory_mb)),
            timestamp=timestamp,
        )
    )
    db.commit()
    return True
