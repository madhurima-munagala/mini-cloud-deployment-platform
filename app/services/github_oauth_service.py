"""
GitHub OAuth HTTP integration.

Every call to GitHub's OAuth/REST endpoints lives here, isolated from
app/api/v1/auth.py, so that:
- the router stays thin
- tests monkeypatch exchange_code_for_token() / fetch_github_user() /
  build_authorize_url() directly instead of making real HTTP calls

Every URL built in this module goes through urllib.parse.urlencode()
rather than manual string concatenation, so query parameters — the
OAuth `state`, redirect URI, etc. — are always correctly encoded.
"""

from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.auth_exceptions import ConfigurationError, GitHubAPIError, GitHubOAuthError

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_API_URL = "https://api.github.com/user"

_HTTP_TIMEOUT_SECONDS = 10.0


def _require_oauth_config() -> tuple[str, str, str]:
    """
    Raises ConfigurationError if GitHub OAuth credentials aren't set,
    rather than letting a None silently flow into a request to GitHub.
    """
    if not (
        settings.github_client_id
        and settings.github_client_secret
        and settings.github_redirect_uri
    ):
        raise ConfigurationError(
            "GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and GITHUB_REDIRECT_URI "
            "must all be set to use GitHub OAuth."
        )
    return settings.github_client_id, settings.github_client_secret, settings.github_redirect_uri


def build_authorize_url(state: str) -> str:
    """
    Builds the GitHub OAuth "authorize" URL. Query parameters are
    encoded via urlencode() — never hand-concatenated — so the `state`
    value (untrusted-until-verified) and redirect_uri are always safe.
    """
    client_id, _client_secret, redirect_uri = _require_oauth_config()

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": settings.github_oauth_scope,
            "state": state,
        }
    )
    return f"{GITHUB_AUTHORIZE_URL}?{query}"


async def exchange_code_for_token(code: str) -> str:
    """
    Exchanges an OAuth `code` for a GitHub access token.

    Raises GitHubOAuthError on any failure (network error, non-200,
    missing access_token in the response) — never returns a partial or
    invalid token.
    """
    client_id, client_secret, redirect_uri = _require_oauth_config()

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                GITHUB_TOKEN_URL,
                data=payload,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("Could not reach GitHub to exchange the OAuth code.") from exc

    if response.status_code != 200:
        raise GitHubOAuthError(
            f"GitHub token exchange failed with status {response.status_code}."
        )

    body = response.json()
    access_token = body.get("access_token")
    if not access_token:
        raise GitHubOAuthError("GitHub token exchange response did not include an access_token.")

    return access_token


async def fetch_github_user(access_token: str) -> dict:
    """
    Fetches the authenticated GitHub user's profile.

    Raises GitHubAPIError on any failure. Returns only the fields this
    application actually uses (github_id, username, avatar_url) — not
    the raw GitHub response, so callers can't accidentally depend on
    fields we haven't decided to support.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                GITHUB_USER_API_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
    except httpx.HTTPError as exc:
        raise GitHubAPIError("Could not reach the GitHub API to fetch the user profile.") from exc

    if response.status_code != 200:
        raise GitHubAPIError(f"GitHub user API request failed with status {response.status_code}.")

    body = response.json()
    github_id = body.get("id")
    username = body.get("login")
    if github_id is None or username is None:
        raise GitHubAPIError("GitHub user API response was missing required fields.")

    return {
        "github_id": github_id,
        "username": username,
        "avatar_url": body.get("avatar_url"),
    }
