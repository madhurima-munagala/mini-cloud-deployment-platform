"""
GitHub repository-listing HTTP integration.

Isolated from app/api/v1/repositories.py so the router stays thin and
tests can mock list_repositories_for_user() directly instead of making
real network calls — same pattern as app/services/github_oauth_service.py.

Never logs or returns the access token. Every error message raised here
is generic and safe to surface to the client — never the token, never
the raw GitHub response body.
"""

from datetime import datetime

import httpx

from app.services.auth_exceptions import GitHubAPIError

GITHUB_USER_REPOS_URL = "https://api.github.com/user/repos"

_HTTP_TIMEOUT_SECONDS = 10.0


def _parse_github_timestamp(value: str | None) -> datetime | None:
    """GitHub returns ISO 8601 timestamps like '2024-05-01T12:00:00Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def list_repositories_for_user(
    access_token: str, *, page: int, per_page: int
) -> list[dict]:
    """
    Fetches one page of the authenticated GitHub user's PUBLIC
    repositories. `visibility=public` is passed to GitHub itself (not
    just filtered client-side afterward) — enforces the v1
    public-repos-only scope at the source.

    Raises GitHubAPIError on any failure (network error, non-2xx
    status, unparseable/unexpected response). The message never
    includes the token or the raw GitHub response body.

    Returns a list of plain dicts containing only the fields this
    application uses — never the raw GitHub payload.
    """
    params = {"page": page, "per_page": per_page, "visibility": "public"}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                GITHUB_USER_REPOS_URL,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
    except httpx.HTTPError as exc:
        raise GitHubAPIError("Could not reach the GitHub API to fetch repositories.") from exc

    if response.status_code == 401:
        raise GitHubAPIError("GitHub rejected the stored access token.")
    if response.status_code == 403:
        raise GitHubAPIError("GitHub API access was forbidden or rate-limited.")
    if response.status_code == 404:
        raise GitHubAPIError("GitHub repositories endpoint was not found.")
    if response.status_code >= 500:
        raise GitHubAPIError("GitHub API is currently unavailable.")
    if response.status_code != 200:
        raise GitHubAPIError(f"GitHub API request failed with status {response.status_code}.")

    try:
        body = response.json()
    except ValueError as exc:
        raise GitHubAPIError("GitHub API returned an unparseable response.") from exc

    if not isinstance(body, list):
        raise GitHubAPIError("GitHub API returned an unexpected response shape.")

    repositories: list[dict] = []
    for item in body:
        github_repo_id = item.get("id")
        name = item.get("name")
        full_name = item.get("full_name")
        if github_repo_id is None or name is None or full_name is None:
            # Skip a malformed entry rather than failing the whole request.
            continue

        repositories.append(
            {
                "github_repo_id": github_repo_id,
                "name": name,
                "full_name": full_name,
                "private": bool(item.get("private", False)),
                "default_branch": item.get("default_branch") or "main",
                "clone_url": item.get("clone_url") or "",
                "github_updated_at": _parse_github_timestamp(item.get("updated_at")),
            }
        )

    return repositories
