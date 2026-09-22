import secrets

from fastapi import Header, HTTPException

from deployment_engine.config import INTERNAL_API_TOKEN


def verify_internal_token(
    x_internal_token: str | None = Header(default=None),
) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Deployment engine internal token is not configured.",
        )

    if not x_internal_token or not secrets.compare_digest(
        x_internal_token,
        INTERNAL_API_TOKEN,
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid internal token.",
        )