"""
repositories — DATABASE_DESIGN.md §2.3

Caches what GET /repositories serves and what POST /deployments validates
against. `private` is stored (not assumed false) so the deployment
creation flow can independently re-check REPO_PRIVATE_NOT_SUPPORTED even
against stale cached data, per API_CONTRACT.md §4.1 and the v1 public-repos-only
scope decision (§3).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        # A given GitHub repo is cached at most once per platform user.
        UniqueConstraint("user_id", "github_repo_id", name="uq_repositories_user_id_github_repo_id"),
    )

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

    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(500), nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    default_branch: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("'main'")
    )
    clone_url: Mapped[str] = mapped_column(Text, nullable=False)

    # GitHub's own updated_at for this repo, used to detect cache staleness.
    # Distinct from our own created_at/updated_at below.
    github_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Repository → User (N:1)
    user: Mapped["User"] = relationship("User", back_populates="repositories")  # noqa: F821

    # Repository → Deployments (1:N)
    deployments: Mapped[list["Deployment"]] = relationship(  # noqa: F821
        "Deployment",
        back_populates="repository",
        cascade="all, delete-orphan",
    )

    # Repository → Environment Variables, both repo-level defaults and any
    # deployment-override snapshots underneath it (1:N)
    environment_variables: Mapped[list["EnvironmentVariable"]] = relationship(  # noqa: F821
        "EnvironmentVariable",
        back_populates="repository",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Repository id={self.id} full_name={self.full_name!r}>"
