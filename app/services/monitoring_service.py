"""
Monitoring metrics business logic — read side only
(GET /deployments/{id}/metrics, §7.1). Ingestion (the engine's callback,
§9.4) writes through deployment_callback_service.append_metric() — the
same read/write split already used for deployment_logs
(deployment_service.list_deployment_logs vs
deployment_callback_service.append_logs).

Ownership is enforced via deployment_service.get_deployment() — the
same join-through-Repository.user_id check used everywhere else in the
project, not duplicated here.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.monitoring_metric import MonitoringMetric
from app.models.user import User
from app.services import deployment_service


class DeploymentNotFoundError(Exception):
    """Raised when deployment_id doesn't exist or isn't owned by the user."""


class MetricsNotAvailableError(Exception):
    """Raised when the deployment exists but has no metric samples yet —
    matches API_CONTRACT.md §7.1's example ("no metrics yet, e.g.
    deployment still pending")."""


def get_deployment_metrics(
    db: DBSession,
    user: User,
    deployment_id: uuid.UUID,
    *,
    since: datetime | None,
    limit: int,
) -> dict:
    """
    Returns the §7.1 response shape as a plain dict:
      {"deployment_id", "container_status", "latest": {...}, "history": [...]}

    `latest` is always the single most recent sample, regardless of
    `since` — `since`/`limit` only narrow `history`. The top-level
    `container_status` mirrors `latest`'s own value (the contract's
    example shows them matching).

    Raises DeploymentNotFoundError if missing/not owned by `user`
    (caller maps to 404 DEPLOYMENT_NOT_FOUND), or
    MetricsNotAvailableError if the deployment exists but has zero
    samples ever, regardless of `since`/`limit` (caller maps to 404
    METRICS_NOT_AVAILABLE).
    """
    deployment = deployment_service.get_deployment(db, user, deployment_id)
    if deployment is None:
        raise DeploymentNotFoundError()

    latest_row = db.execute(
        select(MonitoringMetric)
        .where(MonitoringMetric.deployment_id == deployment_id)
        .order_by(MonitoringMetric.timestamp.desc(), MonitoringMetric.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest_row is None:
        raise MetricsNotAvailableError()

    history_query = (
        select(MonitoringMetric)
        .where(MonitoringMetric.deployment_id == deployment_id)
        .order_by(MonitoringMetric.timestamp.asc(), MonitoringMetric.id.asc())
    )
    if since is not None:
        # Inclusive (>=), matching the same reasoning already established
        # for deployment_logs' `since` filter (Stage 7): the engine can
        # report a batch of samples close enough together to share a
        # timestamp, so an exclusive filter risks dropping a tied sample
        # at the polling boundary.
        history_query = history_query.where(MonitoringMetric.timestamp >= since)
    history_query = history_query.limit(limit)

    history_rows = list(db.execute(history_query).scalars().all())

    return {
        "deployment_id": deployment_id,
        "container_status": latest_row.container_status,
        "latest": {
            "cpu_percent": latest_row.cpu_percent,
            "memory_mb": latest_row.memory_mb,
            "timestamp": latest_row.timestamp,
        },
        "history": [
            {
                "cpu_percent": row.cpu_percent,
                "memory_mb": row.memory_mb,
                "timestamp": row.timestamp,
            }
            for row in history_rows
        ],
    }
