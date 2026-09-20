"""
Application configuration.

All values here come from environment variables (optionally loaded from a
local .env file for development). Nothing sensitive is hardcoded:

- DATABASE_URL is required — no default connection string is baked in.
- ENCRYPTION_KEY is required — no default key is baked in.

See .env.example for the variables this project expects, and
DATABASE_SETUP.md for how to set them locally.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Environment-driven settings for the database layer.

    Only database/encryption configuration is defined here for now — this
    file will grow (GitHub OAuth client id, session cookie settings, etc.)
    once those layers are implemented. Nothing outside the database layer
    is added yet, per the current implementation scope.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # PostgreSQL connection string.
    # Example: postgresql+psycopg2://user:password@localhost:5432/mini_cloud_platform
    database_url: str = Field(..., alias="DATABASE_URL")

    # Optional separate database for running the test suite against, so
    # tests never run against a developer's real data. Falls back to
    # database_url if not provided.
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")

    # Fernet symmetric key used to encrypt/decrypt environment_variables.value_encrypted.
    # Must be a valid Fernet key (32 url-safe base64-encoded bytes).
    # Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = Field(..., alias="ENCRYPTION_KEY")

    # Echo raw SQL to stdout — useful for local debugging, must stay off by
    # default so we don't accidentally log sensitive values via query params.
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    # --- Stage 2 (FastAPI foundation) additions ---
    # All optional/defaulted — none of these become newly-required
    # environment variables, so existing .env files keep working unchanged.

    environment: str = Field(default="development", alias="ENVIRONMENT")
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")

    # Comma-separated list of allowed CORS origins, e.g.
    # "http://localhost:5173,http://localhost:3000". Empty by default,
    # which means CORS middleware is not added at all — see app/main.py.
    # Kept as a plain comma-separated string (not a JSON list) to avoid
    # pydantic-settings' env-var-as-JSON parsing complexity for something
    # this simple.
    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]

    # --- Stage 3 (GitHub OAuth + sessions) additions ---
    # GitHub OAuth credentials are deliberately OPTIONAL here (default
    # None) rather than required, so Settings() still constructs
    # successfully for anyone (including the existing Stage 1/2 test
    # suite) whose .env doesn't set them. The auth endpoints check for
    # their presence at request time and return 500 CONFIGURATION_ERROR
    # if missing, per API_CONTRACT.md's listed error cases — failing
    # loudly at the one place it matters, not at app startup.
    github_client_id: str | None = Field(default=None, alias="GITHUB_CLIENT_ID")
    github_client_secret: str | None = Field(default=None, alias="GITHUB_CLIENT_SECRET")
    github_redirect_uri: str | None = Field(default=None, alias="GITHUB_REDIRECT_URI")

    # GitHub's OAuth scope parameter is space-delimited (not
    # comma-delimited) per GitHub's own documentation — using a space
    # here so the login flow actually works against the real GitHub API.
    github_oauth_scope: str = Field(default="public_repo read:user", alias="GITHUB_OAUTH_SCOPE")

    # Where to redirect the browser after login succeeds/fails. Defaults
    # to a common local Vite dev server port — override in .env once
    # Kay's frontend has a real URL.
    frontend_base_url: str = Field(default="http://localhost:5173", alias="FRONTEND_BASE_URL")

    session_cookie_name: str = Field(default="session", alias="SESSION_COOKIE_NAME")
    session_ttl_seconds: int = Field(default=604800, alias="SESSION_TTL_SECONDS")  # 7 days

    oauth_state_cookie_name: str = Field(default="oauth_state", alias="OAUTH_STATE_COOKIE_NAME")
    oauth_state_ttl_seconds: int = Field(default=600, alias="OAUTH_STATE_TTL_SECONDS")  # 10 minutes

    @property
    def session_cookie_secure(self) -> bool:
        """
        Secure=True everywhere except when ENVIRONMENT is explicitly
        "development" (the default). This is NOT a global removal of the
        Secure flag — deploying with ENVIRONMENT set to anything else
        (e.g. "production", "staging") automatically restores Secure=True
        with no code change and nothing else to remember to flip.
        """
        return self.environment != "development"

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _no_placeholder_credentials(cls, value: str | None) -> str | None:
        """
        Fails fast if someone commits the literal placeholder from
        .env.example instead of a real value — cheap safety net, not a
        replacement for keeping .env out of git.
        """
        if value and "replace-with" in value:
            raise ValueError(
                "DATABASE_URL/TEST_DATABASE_URL still contains a placeholder "
                "value from .env.example. Set a real connection string."
            )
        return value

    @property
    def resolved_test_database_url(self) -> str:
        return self.test_database_url or self.database_url

    # --- Stage 6 (deployment engine integration) additions ---
    # Deliberately OPTIONAL, same reasoning as the GitHub OAuth fields:
    # Settings() must keep constructing successfully for the existing
    # test suite even when these aren't set. Checked at call time by
    # deployment_engine_client.py / app/api/internal.py, not at startup.

    # Base URL of Sreelekha's deployment engine, e.g. http://engine-host:port
    engine_base_url: str | None = Field(default=None, alias="ENGINE_BASE_URL")

    # Shared secret used BOTH directions on the internal channel: sent as
    # X-Internal-Token when the backend calls the engine, and required on
    # every inbound call to /internal/engine-callback/*.
    internal_api_token: str | None = Field(default=None, alias="INTERNAL_API_TOKEN")

    # This backend's own publicly-reachable base URL, given to the engine
    # so it knows where to send callbacks. Defaults to local dev.
    backend_base_url: str = Field(default="http://localhost:8000", alias="BACKEND_BASE_URL")


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Using a function (instead of a module-level
    singleton) makes it easy to override settings in tests via
    dependency/monkeypatch if needed later.
    """
    return Settings()


settings = get_settings()
