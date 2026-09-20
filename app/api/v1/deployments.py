"""
POST /api/v1/deployments
GET  /api/v1/deployments
GET  /api/v1/deployments/{id}
GET  /api/v1/deployments/{id}/logs
GET  /api/v1/deployments/{id}/metrics

Authentication: the EXISTING session-cookie dependency
(app.api.deps.get_current_session) — no JWT, no Authorization header.

Stage 6: creating a deployment now schedules a background call to the
deployment engine (app.services.deployment_engine_client.run_engine_trigger)
after the 201 response's own DB commit — the request never blocks on the
engine.

Stage 8: POST /deployments accepts an optional `env_vars` field — a
deployment-level override snapshot, validated here (before any DB write)
and written atomically with the deployment row by
deployment_service.create_deployment(). There is no read/update/delete
endpoint for these overrides — see
app/services/environment_variable_service.py for why.

Stage 9: GET .../metrics (§7.1) is read-only here, same as .../logs —
ingestion happens only via POST /internal/engine-callback/metrics
(app/api/internal.py). Docker/EC2/container-lifecycle work itself
remains entirely out of scope: this router only persists/reads rows and
asks the engine to start, nothing more.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_session
from app.core.database import get_db
from app.core.exceptions import error_response
from app.models.session import Session as SessionModel
from app.schemas.deployment import DeploymentCreate, DeploymentLogOut, DeploymentOut
from app.schemas.monitoring import MonitoringMetricsOut
from app.services import (
    deployment_engine_client,
    deployment_service,
    environment_variable_service,
    monitoring_service,
)
from app.services.deployment_service import DeploymentInProgressError, RepositoryNotFoundError
from app.services.environment_variable_service import InvalidEnvVarError

router = APIRouter()


def _unauthenticated():
    return error_response(
        code="NOT_AUTHENTICATED",
        message="You must be logged in to access this resource.",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def _deployment_not_found():
    return error_response(
        code="DEPLOYMENT_NOT_FOUND",
        message="Deployment not found.",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _deployment_payload(deployment) -> dict:
    return DeploymentOut.from_deployment(deployment).model_dump(mode="json")


def _invalid_env_var(exc: InvalidEnvVarError):
    return error_response(
        code="INVALID_ENV_VAR",
        message=(
            "Environment variable keys must be uppercase letters, numbers, "
            "and underscores only, and must not repeat within one request."
        ),
        details={"invalid_keys": exc.invalid_keys},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_deployment(
    payload: DeploymentCreate,
    background_tasks: BackgroundTasks,
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    # Validated BEFORE any DB write — a validation failure here must
    # never reach create_deployment(), so it can never leave a partial
    # write behind.
    env_vars_dicts = None
    if payload.env_vars is not None:
        env_vars_dicts = [item.model_dump() for item in payload.env_vars]
        try:
            environment_variable_service.validate_env_vars(env_vars_dicts)
        except InvalidEnvVarError as exc:
            return _invalid_env_var(exc)

    try:
        deployment = deployment_service.create_deployment(
            db,
            session.user,
            repository_id=payload.repository_id,
            branch=payload.branch,
            env_vars=env_vars_dicts,
        )
    except RepositoryNotFoundError:
        return error_response(
            code="REPO_NOT_FOUND",
            message="The requested repository does not exist or you do not have access to it.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except DeploymentInProgressError as exc:
        existing_id = exc.existing_deployment_id
        return error_response(
            code="DEPLOYMENT_IN_PROGRESS",
            message="A deployment for this repository is already running.",
            details=(
                {"existing_deployment_id": str(existing_id)} if existing_id is not None else None
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    # Scheduled AFTER the deployment row (and, if provided, its env var
    # override snapshot) are already committed atomically and the 201
    # response is ready — the request never blocks on the engine.
    # clone_url comes from the existing repository relationship, so
    # deployment_service.create_deployment doesn't need to return it
    # separately. Effective env vars are resolved and decrypted inside
    # run_engine_trigger itself, not here — see that function.
    background_tasks.add_task(
        deployment_engine_client.run_engine_trigger,
        deployment.id,
        clone_url=deployment.repository.clone_url,
        branch=deployment.branch,
    )

    return {"data": _deployment_payload(deployment)}


@router.get("")
def list_deployments(
    repository_id: uuid.UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    deployments, has_next = deployment_service.list_deployments(
        db,
        session.user,
        repository_id=repository_id,
        status=status_filter,
        page=page,
        per_page=per_page,
    )
    data = [_deployment_payload(d) for d in deployments]
    return {"data": data, "meta": {"page": page, "per_page": per_page, "has_next": has_next}}


@router.get("/{deployment_id}")
def get_deployment(
    deployment_id: uuid.UUID,
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    deployment = deployment_service.get_deployment(db, session.user, deployment_id)
    if deployment is None:
        return _deployment_not_found()

    return {"data": _deployment_payload(deployment)}


@router.get("/{deployment_id}/logs")
def get_deployment_logs(
    deployment_id: uuid.UUID,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    logs = deployment_service.list_deployment_logs(
        db, session.user, deployment_id, since=since, limit=limit
    )
    if logs is None:
        return _deployment_not_found()

    data = [DeploymentLogOut.model_validate(log).model_dump(mode="json") for log in logs]
    return {"data": data}


@router.get("/{deployment_id}/metrics")
def get_deployment_metrics(
    deployment_id: uuid.UUID,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=60, ge=1, le=1000),
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    try:
        result = monitoring_service.get_deployment_metrics(
            db, session.user, deployment_id, since=since, limit=limit
        )
    except monitoring_service.DeploymentNotFoundError:
        return _deployment_not_found()
    except monitoring_service.MetricsNotAvailableError:
        return error_response(
            code="METRICS_NOT_AVAILABLE",
            message="No monitoring data available yet for this deployment.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return {"data": MonitoringMetricsOut.model_validate(result).model_dump(mode="json")}
