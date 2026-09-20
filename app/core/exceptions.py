"""
Shared error-response foundation.

Every error returned by this API, in every stage, must match the shape
locked in API_CONTRACT.md §1:

    {"error": {"code": "...", "message": "...", "details": null}}

This module defines that shape once, plus two generic handlers so it's
in place before any endpoint-specific error codes exist:

- RequestValidationError -> 422 VALIDATION_ERROR
  (replaces FastAPI's default validation error body, which does not
  match the contract's shape)
- any unhandled Exception -> 500 INTERNAL_SERVER_ERROR
  (so no endpoint can ever leak a raw traceback in a response body)

Endpoint-specific error codes (REPO_NOT_FOUND, DEPLOYMENT_IN_PROGRESS,
GITHUB_API_ERROR, etc.) are intentionally NOT defined here — they belong
to the endpoints that raise them, which don't exist yet in this stage.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def error_response(
    code: str,
    message: str,
    details: Any = None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> JSONResponse:
    """
    Builds a JSONResponse in the exact shape required by
    API_CONTRACT.md §1. Endpoints in later stages can import and use
    this directly instead of hand-rolling the error body.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return error_response(
        code="VALIDATION_ERROR",
        message="The request could not be validated.",
        details=exc.errors(),
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        code="INTERNAL_SERVER_ERROR",
        message="An unexpected error occurred.",
        details=None,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Called once from app/main.py at startup."""
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
