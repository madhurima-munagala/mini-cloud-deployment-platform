"""
POST /internal/engine-callback/status
POST /internal/engine-callback/logs
POST /internal/engine-callback/metrics

Called by Sreelekha's deployment engine, never by the frontend. Mounted
directly on the app (app/main.py) under /internal — a separate namespace
from /api/v1, matching the locked internal contract's callback_base_url
shape exactly.

Authentication is the X-Internal-Token shared secret, checked BEFORE any
callback body is parsed or validated — not the session cookie, since
this is service-to-service, not user auth. In production this route is
also expected to be network-restricted (private subnet / security
group); the token check here is defense in depth, not the only
protection.

Ordering guarantee: require_internal_token() is a genuine FastAPI
Depends() dependency, and neither route below declares its request body
as an auto-parsed Pydantic parameter. FastAPI resolves declared
dependencies and any typed body parameter together in the same pass —
if a body parameter were declared directly (as in an earlier version of
this file), a malformed body could trigger a 422 RequestValidationError
before the function body — and therefore the token check inside it —
ever ran. Parsing the body manually, strictly after checking
require_internal_token()'s result, removes that race entirely: the
token is always checked first, in every case, including when the body
is also invalid.

JSON-safety: manually validating the body (required for the ordering
guarantee above) means pydantic's ValidationError, not FastAPI's own
RequestValidationError, is what gets caught here — and
ValidationError.errors() can embed a raw exception object in 'ctx',
which plain JSON serialization can't handle. See _json_safe_errors()
below.
"""

import json
import secrets

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import error_response
from app.schemas.deployment import EngineLogsCallback, EngineStatusCallback
from app.schemas.monitoring import EngineMetricsCallback
from app.services import deployment_callback_service

router = APIRouter()


def require_internal_token(
    x_internal_token: str | None = Header(default=None),
) -> JSONResponse | None:
    """
    FastAPI dependency: returns an error_response() if the token is
    missing/invalid/unconfigured, else None. Declared via Depends() on
    both routes below and checked as the very first thing each handler
    does — before the request body is read or parsed at all.
    """
    if not settings.internal_api_token:
        return error_response(
            code="CONFIGURATION_ERROR",
            message="The internal engine-callback channel is not configured on this server.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, settings.internal_api_token
    ):
        return error_response(
            code="INTERNAL_TOKEN_INVALID",
            message="Missing or invalid internal token.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    return None


def _json_safe_errors(exc: ValidationError) -> list:
    """
    pydantic v2's ValidationError.errors() can embed non-JSON-serializable
    objects in each error's 'ctx' — notably the raw exception instance
    itself when a @field_validator raises a bare ValueError (exactly
    what EngineStatusCallback/EngineLogEntry's validators do), which
    crashes plain JSON serialization with
    "Object of type ValueError is not JSON serializable".

    Round-tripping through json.dumps(..., default=str) — the same
    fallback strategy FastAPI's own jsonable_encoder uses for values it
    doesn't otherwise recognize — converts any such object to its string
    form instead of failing, while leaving already-serializable values
    untouched.
    """
    return json.loads(json.dumps(exc.errors(), default=str))


def _invalid_body_response(exc: Exception) -> JSONResponse:
    """
    Mirrors app/core/exceptions.py's validation_exception_handler shape
    exactly (same code, same message) so manually validating the body
    here — required to guarantee the auth-before-body ordering above —
    doesn't produce a different error shape than the rest of the API.
    Unlike that handler, `details` is passed through _json_safe_errors()
    first (see its docstring for why).
    """
    details = _json_safe_errors(exc) if isinstance(exc, ValidationError) else None
    return error_response(
        code="VALIDATION_ERROR",
        message="The request could not be validated.",
        details=details,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.post("/engine-callback/status")
async def engine_status_callback(
    request: Request,
    auth_error: JSONResponse | None = Depends(require_internal_token),
    db: DBSession = Depends(get_db),
):
    if auth_error is not None:
        return auth_error

    try:
        raw_body = await request.json()
    except ValueError as exc:
        return _invalid_body_response(exc)

    try:
        payload = EngineStatusCallback.model_validate(raw_body)
    except ValidationError as exc:
        return _invalid_body_response(exc)

    deployment = deployment_callback_service.apply_status_update(
        db,
        deployment_id=payload.deployment_id,
        status=payload.status,
        live_url=payload.live_url,
        container_id=payload.container_id,
        error_message=payload.error_message,
    )
    if deployment is None:
        return error_response(
            code="DEPLOYMENT_NOT_FOUND",
            message="Deployment not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return {"received": True}


@router.post("/engine-callback/logs")
async def engine_logs_callback(
    request: Request,
    auth_error: JSONResponse | None = Depends(require_internal_token),
    db: DBSession = Depends(get_db),
):
    if auth_error is not None:
        return auth_error

    try:
        raw_body = await request.json()
    except ValueError as exc:
        return _invalid_body_response(exc)

    try:
        payload = EngineLogsCallback.model_validate(raw_body)
    except ValidationError as exc:
        return _invalid_body_response(exc)

    count = deployment_callback_service.append_logs(
        db,
        deployment_id=payload.deployment_id,
        logs=[entry.model_dump() for entry in payload.logs],
    )
    if count is None:
        return error_response(
            code="DEPLOYMENT_NOT_FOUND",
            message="Deployment not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return {"received": True, "count": count}


@router.post("/engine-callback/metrics")
async def engine_metrics_callback(
    request: Request,
    auth_error: JSONResponse | None = Depends(require_internal_token),
    db: DBSession = Depends(get_db),
):
    if auth_error is not None:
        return auth_error

    try:
        raw_body = await request.json()
    except ValueError as exc:
        return _invalid_body_response(exc)

    try:
        payload = EngineMetricsCallback.model_validate(raw_body)
    except ValidationError as exc:
        return _invalid_body_response(exc)

    inserted = deployment_callback_service.append_metric(
        db,
        deployment_id=payload.deployment_id,
        container_status=payload.container_status,
        cpu_percent=payload.cpu_percent,
        memory_mb=payload.memory_mb,
        timestamp=payload.timestamp,
    )
    if not inserted:
        return error_response(
            code="DEPLOYMENT_NOT_FOUND",
            message="Deployment not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return {"received": True}
