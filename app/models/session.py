"""
sessions — DATABASE_DESIGN.md §2.2

Backs the cookie-based authentication finalized in API_CONTRACT.md §2.
Only the HASH of the session cookie value is stored — never the raw
value — so a database leak alone cannot be used to forge a session
(same principle as password hashing).
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Hash of the opaque cookie value handed to the browser. The raw value
    # is never persisted.
    session_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Session → User (N:1)
    user: Mapped["User"] = relationship("User", back_populates="sessions")  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Session id={self.id} user_id={self.user_id}>"
