"""
deployment_logs — DATABASE_DESIGN.md §2.6

Append-only, high-volume table fed by the engine's batched log callback
(API_CONTRACT.md §9.3) and read by the polled GET /deployments/{id}/logs
endpoint (§4.4). Uses a BIGSERIAL primary key instead of UUID, matching
the design's reasoning: cheaper writes/indexes for a table that grows
continuously during active deployments.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base

LOG_LEVELS: tuple[str, ...] = ("info", "warn", "error")


class DeploymentLog(Base):
    __tablename__ = "deployment_logs"
    __table_args__ = (
        CheckConstraint("level IN ('info','warn','error')", name="ck_deployment_logs_level"),
        # Core polling query: GET /deployments/{id}/logs?since=...
        Index("ix_deployment_logs_deployment_id_timestamp", "deployment_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The log event's own time, as reported by the engine.
    timestamp: Mapped[datetime] = mapped_column(nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # When our backend actually stored it — distinct from `timestamp`,
    # useful for diagnosing callback delivery delays.
    received_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    # DeploymentLog → Deployment (N:1)
    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        "Deployment", back_populates="logs"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<DeploymentLog id={self.id} deployment_id={self.deployment_id} level={self.level!r}>"
