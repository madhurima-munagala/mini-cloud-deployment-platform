"""
Schemas for GET /deployments/{id}/metrics (§7.1) and the engine's
metrics callback, POST /internal/engine-callback/metrics (§9.4).

cpu_percent/memory_mb are declared as `float` (not Decimal) so
model_dump(mode="json") emits a plain JSON number (e.g. 12.4), matching
§7.1's example exactly — Pydantic v2 serializes Decimal as a string in
JSON mode by default, which would not match the contract's shown shape.
The underlying DB column (app/models/monitoring_metric.py) stays
NUMERIC; conversion happens at the service boundary
(deployment_callback_service.append_metric), not here.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.models.monitoring_metric import CONTAINER_STATUSES


class MonitoringMetricPoint(BaseModel):
    cpu_percent: float
    memory_mb: float
    timestamp: datetime


class MonitoringMetricsOut(BaseModel):
    deployment_id: uuid.UUID
    container_status: str
    latest: MonitoringMetricPoint
    history: list[MonitoringMetricPoint]


class EngineMetricsCallback(BaseModel):
    deployment_id: uuid.UUID
    container_status: str
    cpu_percent: float
    memory_mb: float
    timestamp: datetime

    @field_validator("container_status")
    @classmethod
    def _container_status_must_be_known(cls, value: str) -> str:
        if value not in CONTAINER_STATUSES:
            raise ValueError(f"container_status must be one of {CONTAINER_STATUSES}")
        return value
