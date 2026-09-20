"""
monitoring_metrics — DATABASE_DESIGN.md §2.7

Append-only, high-volume table fed by the engine's metrics callback
(API_CONTRACT.md §9.4) and read by the polled
GET /deployments/{id}/metrics endpoint (§7.1). Uses a BIGSERIAL primary
key for the same reason as deployment_logs.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base

CONTAINER_STATUSES: tuple[str, ...] = ("running", "stopped", "crashed", "unknown")


class MonitoringMetric(Base):
    __tablename__ = "monitoring_metrics"
    __table_args__ = (
        CheckConstraint(
            "container_status IN ('running','stopped','crashed','unknown')",
            name="ck_monitoring_metrics_container_status",
        ),
        # Core polling query: GET /deployments/{id}/metrics?since=...
        Index("ix_monitoring_metrics_deployment_id_timestamp", "deployment_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )

    container_status: Mapped[str] = mapped_column(String(10), nullable=False)
    cpu_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    memory_mb: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # The metric sample's own time, as reported by the engine.
    timestamp: Mapped[datetime] = mapped_column(nullable=False)

    received_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    # MonitoringMetric → Deployment (N:1)
    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        "Deployment", back_populates="metrics"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<MonitoringMetric id={self.id} deployment_id={self.deployment_id} "
            f"cpu_percent={self.cpu_percent} memory_mb={self.memory_mb}>"
        )
