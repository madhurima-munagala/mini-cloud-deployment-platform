"""
Backend -> deployment engine HTTP integration.

Mirrors the existing async httpx.AsyncClient + timeout + raise-on-failure
pattern already used in app/services/github_oauth_service.py.

trigger_deployment() is the pure HTTP call: POST {ENGINE_BASE_URL}/deploy
with the X-Internal-Token shared secret, using the exact field names from
the locked internal contract.

run_engine_trigger() is the BackgroundTasks entrypoint scheduled by
app/api/v1/deployments.py after a deployment is created. It opens its
OWN database session (SessionLocal, not the request's — the request's
session is already closed by the time a background task runs) and, on
any failure, marks the deployment "failed" via
deployment_callback_service so the same failure path the engine's own
status callback would use is reused here too.

Stage 8: run_engine_trigger now ALSO resolves the deployment's effective
environment variables (override > repository default — see
app/services/environment_variable_service.py:resolve_effective_env_vars)
and decrypts them, immediately before building the outbound payload.
This is the only place decrypted env var values ever exist outside the
database — never logged, never returned, discarded as soon as the HTTP
call completes. A database session is therefore now opened
unconditionally at the top of run_engine_trigger (previously only
opened in the failure branch), since resolving env vars requires a read
regardless of whether the trigger call itself succeeds or fails.
"""

import uuid

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.deployment import Deployment
from app.services import deployment_callback_service, environment_variable_service

_HTTP_TIMEOUT_SECONDS = 10.0

_ENGINE_UNREACHABLE_MESSAGE = "Deployment engine unreachable"


class EngineError(Exception):
    """Base class for deployment-engine trigger errors."""


class EngineConfigurationError(EngineError):
    """Raised when ENGINE_BASE_URL or INTERNAL_API_TOKEN isn't configured."""


class EngineUnavailableError(EngineError):
    """Raised on network error, timeout, or a 5xx response from the engine."""


class EngineRejectedError(EngineError):
    """Raised when the engine responds with a 4xx (e.g. busy/invalid)."""


def _require_engine_config() -> tuple[str, str]:
    if not (settings.engine_base_url and settings.internal_api_token):
        raise EngineConfigurationError(
            "ENGINE_BASE_URL and INTERNAL_API_TOKEN must both be set to trigger a deployment."
        )
    return settings.engine_base_url, settings.internal_api_token


async def trigger_deployment(
    deployment_id: uuid.UUID, *, clone_url: str, branch: str, env_vars: list[dict]
) -> str:
    """
    Calls the engine's /deploy endpoint. `env_vars` must already be the
    resolved, DECRYPTED effective set (see
    environment_variable_service.resolve_effective_env_vars) — this
    function does no resolution or decryption itself, it only forwards
    what it's given.

    Returns the engine's engine_job_id (not persisted — deployment_id is
    the sole correlation key for all callbacks). Raises
    EngineConfigurationError / EngineUnavailableError / EngineRejectedError
    on failure; never returns a partial/invalid result.
    """
    engine_base_url, internal_api_token = _require_engine_config()

    payload = {
        "deployment_id": str(deployment_id),
        "repository": {"clone_url": clone_url, "branch": branch},
        "env_vars": env_vars,
        "callback_base_url": f"{settings.backend_base_url.rstrip('/')}/internal/engine-callback",
    }
    headers = {"X-Internal-Token": internal_api_token}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{engine_base_url.rstrip('/')}/deploy", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        raise EngineUnavailableError("Could not reach the deployment engine.") from exc

    if response.status_code >= 500:
        raise EngineUnavailableError("Deployment engine returned a server error.")
    if 400 <= response.status_code < 500:
        raise EngineRejectedError(f"Deployment engine rejected the request ({response.status_code}).")
    if response.status_code not in (200, 201, 202):
        raise EngineUnavailableError(
            f"Deployment engine returned an unexpected status ({response.status_code})."
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise EngineUnavailableError("Deployment engine returned an unparseable response.") from exc

    return body.get("engine_job_id", "")


async def run_engine_trigger(deployment_id: uuid.UUID, *, clone_url: str, branch: str) -> None:
    """
    BackgroundTasks entrypoint — scheduled from create_deployment after
    the 201 response's own DB commit has already happened. Never raises:
    any failure here is written to the deployment row instead, so a
    background task exception never surfaces as a server error (there is
    no request left to respond to by the time this runs).
    """
    db = SessionLocal()
    try:
        deployment = db.execute(
            select(Deployment).where(Deployment.id == deployment_id)
        ).scalar_one_or_none()

        # Should not realistically happen (the deployment was just
        # committed moments ago by the request that scheduled this task)
        # but resolving against a missing row would be a bug, not a
        # engine-reachability failure — fail closed with an empty set
        # rather than raising somewhere unexpected in a background task.
        env_vars = (
            environment_variable_service.resolve_effective_env_vars(db, deployment)
            if deployment is not None
            else []
        )

        try:
            await trigger_deployment(
                deployment_id, clone_url=clone_url, branch=branch, env_vars=env_vars
            )
        except EngineError:
            deployment_callback_service.mark_deployment_failed(
                db, deployment_id, _ENGINE_UNREACHABLE_MESSAGE
            )
    finally:
        db.close()
