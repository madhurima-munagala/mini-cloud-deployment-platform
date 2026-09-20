"""
Request/response schemas for the deployment endpoints
(POST/GET /deployments, GET /deployments/{id}, GET /deployments/{id}/logs).

Field names/shapes follow the deployment JSON shape already established
in the project's API contract discussions. No token, secret, or
internal-only field is ever exposed here.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.deployment import DEPLOYMENT_STATUSES
from app.models.deployment_log import LOG_LEVELS
from app.schemas.environment_variable import EnvVarIn


class DeploymentCreate(BaseModel):
    repository_id: uuid.UUID
    # Optional — defaults to the repository's own default_branch if omitted.
    branch: str | None = None
    # Stage 8: optional deployment-level override snapshot. Each item is
    # validated (key format + no duplicates within this list) by
    # app/services/environment_variable_service.py:validate_env_vars()
    # before anything is written — see app/api/v1/deployments.py.
    env_vars: list[EnvVarIn] | None = None


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository_id: uuid.UUID
    # API_CONTRACT.md §4.2/§4.3. Not a column on Deployment — it comes from
    # the related Repository, so build instances with from_deployment()
    # rather than model_validate(deployment).
    repository_name: str
    branch: str
    status: str
    live_url: str | None
    container_id: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_deployment(cls, deployment) -> "DeploymentOut":
        return cls(
            id=deployment.id,
            repository_id=deployment.repository_id,
            repository_name=deployment.repository.name,
            branch=deployment.branch,
            status=deployment.status,
            live_url=deployment.live_url,
            container_id=deployment.container_id,
            error_message=deployment.error_message,
            created_at=deployment.created_at,
            updated_at=deployment.updated_at,
        )


class DeploymentLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Stage 7 addition: lets a client de-duplicate a log line that
    # reappears at a `since` boundary (list_deployment_logs's `since`
    # filter is inclusive — see app/services/deployment_service.py).
    # Exposes the existing DeploymentLog.id column; no DB change.
    id: int
    timestamp: datetime
    level: str
    message: str


# --- Stage 6: engine -> backend callback request bodies ---
# Field names match the locked internal contract exactly:
# POST /internal/engine-callback/status
#   { deployment_id, status, live_url, container_id, error_message, timestamp }
# POST /internal/engine-callback/logs
#   { deployment_id, logs: [{ timestamp, level, message }, ...] }


class EngineStatusCallback(BaseModel):
    deployment_id: uuid.UUID
    status: str
    live_url: str | None = None
    container_id: str | None = None
    error_message: str | None = None
    timestamp: datetime

    @field_validator("status")
    @classmethod
    def _status_must_be_known(cls, value: str) -> str:
        if value not in DEPLOYMENT_STATUSES:
            raise ValueError(f"status must be one of {DEPLOYMENT_STATUSES}")
        return value


class EngineLogEntry(BaseModel):
    timestamp: datetime
    level: str
    message: str

    @field_validator("level")
    @classmethod
    def _level_must_be_known(cls, value: str) -> str:
        if value not in LOG_LEVELS:
            raise ValueError(f"level must be one of {LOG_LEVELS}")
        return value


class EngineLogsCallback(BaseModel):
    deployment_id: uuid.UUID
    logs: list[EngineLogEntry]
