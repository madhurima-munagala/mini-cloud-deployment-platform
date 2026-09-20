"""
Domain-specific exceptions for the GitHub OAuth / session auth flow.

Kept separate from app/core/exceptions.py (which owns the generic,
FastAPI-level error-response shape and handlers) — these are raised by
the service layer and caught explicitly in app/api/v1/auth.py, which
decides the actual HTTP response for each case.
"""


class AuthError(Exception):
    """Base class for authentication-flow errors."""


class ConfigurationError(AuthError):
    """Raised when required GitHub OAuth configuration is missing."""


class OAuthStateError(AuthError):
    """Raised when the OAuth `state` parameter is missing or invalid."""


class GitHubOAuthError(AuthError):
    """Raised when exchanging the OAuth `code` for an access token fails."""


class GitHubAPIError(AuthError):
    """Raised when fetching the GitHub user profile fails."""
