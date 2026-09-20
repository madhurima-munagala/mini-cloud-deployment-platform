"""
FastAPI application entry point.

Creates the app, registers the shared error-handling foundation,
optionally enables CORS if explicitly configured, and mounts:
- the versioned, frontend-facing API under /api/v1 (app/api/v1/__init__.py)
- the internal engine-callback routes under /internal, a separate
  namespace not covered by the /api/v1 prefix or session-cookie auth —
  see app/api/internal.py for why (X-Internal-Token instead).

Run locally with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.internal import router as internal_router
from app.api.v1 import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers

app = FastAPI(
    title="Mini Cloud Deployment Platform API",
    version="0.1.0",
)

register_exception_handlers(app)

# CORS is off by default and only enabled if CORS_ALLOWED_ORIGINS is
# explicitly set — keeps this stage minimal and non-breaking, per
# instruction. No new required environment variable.
if settings.cors_allowed_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        # Credentials (the cookie-based session from API_CONTRACT.md §2)
        # must be allowed for the browser to send/receive the session
        # cookie once auth is implemented in a later stage.
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.api_v1_prefix)
app.include_router(internal_router, prefix="/internal")
