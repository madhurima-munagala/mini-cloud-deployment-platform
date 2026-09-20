"""
Shared FastAPI dependencies for the auth layer.

get_current_session() resolves the "session" cookie (API_CONTRACT.md §2)
into a live Session row (with its related User available via
session.user), or None if the cookie is missing, unrecognized, or
expired.

It deliberately RETURNS None instead of raising an exception, so each
endpoint decides its own 401 response body — the same pattern already
used in app/api/v1/health.py for its DATABASE_UNAVAILABLE case. This
avoids needing a new global exception handler in app/core/exceptions.py
or app/main.py for this stage.
"""

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.core.database import get_db
from app.models.session import Session as SessionModel
from app.services import session_service


def get_current_session(
    request: Request, db: DBSession = Depends(get_db)
) -> SessionModel | None:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None
    return session_service.get_valid_session(db, raw_token)
