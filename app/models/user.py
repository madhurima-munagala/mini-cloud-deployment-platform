"""
users — DATABASE_DESIGN.md §2.1

Root table. No foreign keys. Referenced by sessions.user_id and
repositories.user_id.

Security notes (per project rules):
- github_access_token_encrypted stores the GitHub OAuth ACCESS token
  (used to call the GitHub API on the user's behalf), encrypted at rest.
  It is never returned in any API response.
- The GitHub OAuth CLIENT SECRET is not stored here (or anywhere in the
  database) — it belongs in backend configuration only.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sensitive — encrypted at rest, never serialized to any API response.
    github_access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # User → Sessions (1:N)
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        "Session",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    # User → Repositories (1:N)
    repositories: Mapped[list["Repository"]] = relationship(  # noqa: F821
        "Repository",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        # Deliberately excludes github_access_token_encrypted from repr.
        return f"<User id={self.id} username={self.username!r}>"
