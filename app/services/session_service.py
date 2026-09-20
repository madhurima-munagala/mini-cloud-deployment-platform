"""
Session creation/validation/deletion.

Uses the EXISTING app.models.session.Session model as-is — no schema
changes. Only a SHA-256 hash of the raw session token is ever passed to
the database; the raw token exists only in memory long enough to be
returned to the caller (which sets it as the cookie value) and is never
logged.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.models.session import Session as SessionModel
from app.models.user import User


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(db: DBSession, user: User) -> tuple[str, SessionModel]:
    """
    Creates a new session for `user`. Returns (raw_token, session_row).
    Only session_token_hash is persisted — raw_token is returned so the
    caller can set it as the cookie value, and is not stored anywhere.
    """
    raw_token = secrets.token_urlsafe(32)

    session = SessionModel(
        user_id=user.id,
        session_token_hash=_hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return raw_token, session


def get_valid_session(db: DBSession, raw_token: str) -> SessionModel | None:
    """
    Resolves a raw cookie value to a live Session row, or None if the
    token is unrecognized or the session has expired.

    On success, updates last_used_at and COMMITS the change — mutating
    the in-memory object alone would never reach PostgreSQL, since each
    request's session isn't auto-committed on teardown (see
    app/core/database.py:get_db(), which only closes, never commits).
    """
    token_hash = _hash_token(raw_token)
    session = db.execute(
        select(SessionModel).where(SessionModel.session_token_hash == token_hash)
    ).scalar_one_or_none()

    if session is None:
        return None

    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        # Defensive: TIMESTAMPTZ round-trips as tz-aware via psycopg2 in
        # normal operation, but don't assume it in every driver/config.
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < now:
        return None

    session.last_used_at = now
    db.commit()  # <-- required: persists last_used_at to PostgreSQL, not just the object
    db.refresh(session)

    return session


def delete_session(db: DBSession, session: SessionModel) -> None:
    """Used by POST /auth/logout to invalidate the current session."""
    db.delete(session)
    db.commit()
