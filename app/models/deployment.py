"""
deployments — DATABASE_DESIGN.md §2.4

CRITICAL constraint implemented here: API_CONTRACT.md §4.1's concurrency
policy — "a same-repository deploy is rejected with 409
DEPLOYMENT_IN_PROGRESS while an earlier deployment for that repo is still
pending/building/starting" — is enforced at the DATABASE level via a
PostgreSQL partial unique index, not just an application-level check.
This guarantees correctness even under concurrent requests/race
conditions; the API-layer check (added later) exists only to return a
clean 409 error message before hitting the DB constraint.

The index intentionally does NOT restrict `running`, `failed`, or
`stopped` deployments — a repo can have any number of deployments in
those terminal/live states (that's how deployment history and the
redeploy/replacement flow in API_CONTRACT.md §9.6 work).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base

# Single source of truth for the allowed status values — imported by the
# CHECK constraint below, and reusable by the (not-yet-built) API/service
# layer so the enum never drifts out of sync with the database.
DEPLOYMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "building",
    "starting",
    "running",
    "failed",
    "stopped",
)

# Statuses considered "active" for the one-active-deployment-per-repository rule.
ACTIVE_DEPLOYMENT_STATUSES: tuple[str, ...] = ("pending", "building", "starting")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','building','starting','running','failed','stopped')",
            name="ck_deployments_status",
        ),
        # Partial unique index: at most one row per repository_id where
        # status is one of the "active" values. This is what actually
        # enforces "no two simultaneous active deployments per repo."
        Index(
            "uq_deployments_active_per_repository",
            "repository_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'building', 'starting')"),
        ),
        # Supports GET /deployments history queries ordered by recency,
        # filtered by repository.
        Index("ix_deployments_repository_id_created_at", "repository_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    live_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Deployment → Repository (N:1)
    repository: Mapped["Repository"] = relationship(  # noqa: F821
        "Repository", back_populates="deployments"
    )

    # Deployment → Logs (1:N)
    logs: Mapped[list["DeploymentLog"]] = relationship(  # noqa: F821
        "DeploymentLog",
        back_populates="deployment",
        cascade="all, delete-orphan",
    )

    # Deployment → Metrics (1:N)
    metrics: Mapped[list["MonitoringMetric"]] = relationship(  # noqa: F821
        "MonitoringMetric",
        back_populates="deployment",
        cascade="all, delete-orphan",
    )

    # Deployment → Environment Variable override snapshots (1:N, optional)
    env_var_overrides: Mapped[list["EnvironmentVariable"]] = relationship(  # noqa: F821
        "EnvironmentVariable",
        back_populates="deployment",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Deployment id={self.id} repository_id={self.repository_id} status={self.status!r}>"
