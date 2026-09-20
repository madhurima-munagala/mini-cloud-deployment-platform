"""
GET /api/v1/repositories
GET /api/v1/repositories/{id}/env
PUT /api/v1/repositories/{id}/env

Returns the authenticated user's public GitHub repositories, fetched
live from GitHub and synchronized into the local `repositories` table.

Authentication: the EXISTING session-cookie dependency
(app.api.deps.get_current_session) — no JWT, no Authorization header,
reused as-is from Stage 3.

The GitHub access token is decrypted only in memory, for the duration of
this one request, and is never logged, returned, or stored decrypted.
Ownership is always taken from the authenticated session's user — never
from any request parameter.

Stage 8: GET/PUT .../env manage repository-level default environment
variables. PUT is full-replace (not a per-key patch). Every value
returned by GET or PUT is masked, including non-sensitive keys — see
app/services/environment_variable_service.py's module docstring for why
this is stricter than earlier contract language. Deployment-level
overrides have no endpoint of their own here — see that same docstring.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_session
from app.core.database import get_db
from app.core.exceptions import error_response
from app.models.repository import Repository
from app.models.session import Session as SessionModel
from app.schemas.environment_variable import EnvVarOut, EnvVarSetRequest
from app.schemas.repository import RepositoryOut
from app.services import environment_variable_service, github_repository_service, repository_service
from app.services.auth_exceptions import GitHubAPIError
from app.services.environment_variable_service import InvalidEnvVarError
from app.utils.encryption import EncryptionError, decrypt_value, mask_value

router = APIRouter()


def _unauthenticated():
    return error_response(
        code="NOT_AUTHENTICATED",
        message="You must be logged in to access this resource.",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def _repository_not_found():
    return error_response(
        code="REPO_NOT_FOUND",
        message="The requested repository does not exist or you do not have access to it.",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _owned_repository_or_none(db: DBSession, user, repository_id: uuid.UUID) -> Repository | None:
    return db.execute(
        select(Repository).where(Repository.id == repository_id, Repository.user_id == user.id)
    ).scalar_one_or_none()


def _masked_out(rows) -> list[dict]:
    # Always the fixed mask, regardless of is_sensitive — Stage 8's
    # explicit rule. See environment_variable_service's module docstring.
    return [EnvVarOut(key=row.key, value=mask_value(True)).model_dump() for row in rows]


@router.get("")
async def list_repositories(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    # Ownership comes only from the authenticated session's user — no
    # user_id/username from the request is ever consulted.
    user = session.user

    try:
        access_token = decrypt_value(user.github_access_token_encrypted)
    except EncryptionError:
        # Never include the encrypted/plaintext value in this message.
        return error_response(
            code="INTERNAL_SERVER_ERROR",
            message="Could not access the stored GitHub credentials.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    try:
        github_repos = await github_repository_service.list_repositories_for_user(
            access_token, page=page, per_page=per_page
        )
    except GitHubAPIError:
        # Generic, token-free, response-body-free message regardless of
        # which specific GitHub failure (401/403/404/5xx/network) occurred.
        return error_response(
            code="GITHUB_API_ERROR",
            message="Could not fetch repositories from GitHub. Please try again.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    # `access_token` is not referenced again after this point in this function.

    try:
        synced_repos = repository_service.sync_repositories(db, user, github_repos)
    except Exception:
        # Ensure the failed transaction is rolled back before the error
        # surfaces, then re-raise so the existing generic exception
        # handler (app/core/exceptions.py:unhandled_exception_handler,
        # registered in app/main.py) produces the standard
        # 500 INTERNAL_SERVER_ERROR shape — no duplicate error-shape
        # logic here, and nothing is swallowed silently.
        db.rollback()
        raise

    data = [RepositoryOut.model_validate(repo).model_dump(mode="json") for repo in synced_repos]
    # has_next: GitHub returned a full page, so another page may follow.
    # This is a page-size heuristic — it reads True for a final page that
    # happens to hold exactly `per_page` repositories (the next request
    # then returns an empty page with has_next false).
    has_next = len(github_repos) >= per_page
    return {"data": data, "meta": {"page": page, "per_page": per_page, "has_next": has_next}}


@router.get("/{repository_id}/env")
def get_repository_env(
    repository_id: uuid.UUID,
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    repo = _owned_repository_or_none(db, session.user, repository_id)
    if repo is None:
        return _repository_not_found()

    rows = environment_variable_service.list_repository_defaults(db, repository_id)
    return {"data": _masked_out(rows)}


@router.put("/{repository_id}/env")
def put_repository_env(
    repository_id: uuid.UUID,
    payload: EnvVarSetRequest,
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return _unauthenticated()

    repo = _owned_repository_or_none(db, session.user, repository_id)
    if repo is None:
        return _repository_not_found()

    env_vars_dicts = [item.model_dump() for item in payload.env_vars]
    try:
        environment_variable_service.validate_env_vars(env_vars_dicts)
    except InvalidEnvVarError as exc:
        return error_response(
            code="INVALID_ENV_VAR",
            message=(
                "Environment variable keys must be uppercase letters, numbers, "
                "and underscores only, and must not repeat within one request."
            ),
            details={"invalid_keys": exc.invalid_keys},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    rows = environment_variable_service.replace_repository_defaults(
        db, repository_id, env_vars_dicts
    )
    return {"data": _masked_out(rows)}
