"""
GET  /api/v1/auth/github/login
GET  /api/v1/auth/github/callback
GET  /api/v1/auth/me
POST /api/v1/auth/logout

Cookie-based session authentication only (API_CONTRACT.md §2). No JWT,
no Authorization header, anywhere in this router.

Repository fetching, deployments, and everything past authentication are
explicitly out of scope for this stage.
"""

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_session
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import error_response
from app.models.session import Session as SessionModel
from app.schemas.auth import UserOut
from app.services import github_oauth_service, session_service, user_service
from app.services.auth_exceptions import ConfigurationError, GitHubAPIError, GitHubOAuthError
from app.utils.encryption import encrypt_value

router = APIRouter()


def _frontend_redirect(path: str, params: dict | None = None) -> RedirectResponse:
    """
    Builds a redirect Response to the frontend. The query string, when
    present, always goes through urlencode() — never manual string
    concatenation — so parameters (e.g. `error`) are always safely
    encoded.
    """
    url = settings.frontend_base_url.rstrip("/") + path
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def _set_cookie(response, *, name: str, value: str, max_age: int) -> None:
    """
    Applies the cookie flags required by API_CONTRACT.md §2 and the
    Stage 3 security requirements: HttpOnly, SameSite=Lax, Path=/, and a
    Secure flag driven by settings.session_cookie_secure (True except in
    local "development", never unconditionally disabled).
    """
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/github/login")
def github_login():
    try:
        state = secrets.token_urlsafe(32)
        authorize_url = github_oauth_service.build_authorize_url(state)
    except ConfigurationError:
        return error_response(
            code="CONFIGURATION_ERROR",
            message="GitHub OAuth is not configured on this server.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response = RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
    _set_cookie(
        response,
        name=settings.oauth_state_cookie_name,
        value=state,
        max_age=settings.oauth_state_ttl_seconds,
    )
    return response


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: DBSession = Depends(get_db),
):
    cookie_state = request.cookies.get(settings.oauth_state_cookie_name)

    def _failure_redirect() -> RedirectResponse:
        resp = _frontend_redirect("/login", {"error": "oauth_failed"})
        # Clear the state cookie on every outcome (success or failure) so
        # it can only ever be used once from the browser's perspective.
        resp.delete_cookie(settings.oauth_state_cookie_name, path="/")
        return resp

    # Missing code/state, missing state cookie, or a mismatch are all
    # treated as an invalid/CSRF-suspect callback. secrets.compare_digest
    # is used for a constant-time comparison.
    if (
        not code
        or not state
        or not cookie_state
        or not secrets.compare_digest(state, cookie_state)
    ):
        return _failure_redirect()

    try:
        access_token = await github_oauth_service.exchange_code_for_token(code)
        profile = await github_oauth_service.fetch_github_user(access_token)
    except (GitHubOAuthError, GitHubAPIError, ConfigurationError):
        return _failure_redirect()

    encrypted_token = encrypt_value(access_token)

    user = user_service.get_or_create_user(
        db,
        github_id=profile["github_id"],
        username=profile["username"],
        avatar_url=profile["avatar_url"],
        github_access_token_encrypted=encrypted_token,
    )

    raw_session_token, _session = session_service.create_session(db, user)

    response = _frontend_redirect("/dashboard")
    response.delete_cookie(settings.oauth_state_cookie_name, path="/")
    _set_cookie(
        response,
        name=settings.session_cookie_name,
        value=raw_session_token,
        max_age=settings.session_ttl_seconds,
    )
    return response


@router.get("/me")
def get_me(session: SessionModel | None = Depends(get_current_session)):
    if session is None:
        return error_response(
            code="NOT_AUTHENTICATED",
            message="You must be logged in to access this resource.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    user_out = UserOut.model_validate(session.user)
    return {"data": user_out.model_dump(mode="json")}


@router.post("/logout")
def logout(
    session: SessionModel | None = Depends(get_current_session),
    db: DBSession = Depends(get_db),
):
    if session is None:
        return error_response(
            code="NOT_AUTHENTICATED",
            message="You must be logged in to access this resource.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    session_service.delete_session(db, session)

    response = JSONResponse(
        content={"data": {"logged_out": True}}, status_code=status.HTTP_200_OK
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response
