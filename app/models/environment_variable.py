"""
environment_variables — DATABASE_DESIGN.md §2.5

One table for two use cases, distinguished by whether deployment_id is
NULL:

- deployment_id IS NULL   -> repository-level default (GET/PUT
  /repositories/{id}/env in API_CONTRACT.md §6)
- deployment_id IS NOT NULL -> a snapshot of the value actually used for
  one specific deployment, when POST /deployments included an override
  (API_CONTRACT.md §4.1)

Security: value_encrypted is the ONLY value column — there is no
plaintext `value` column anywhere in this schema. See
app/utils/encryption.py for the encrypt/decrypt/mask utility this table
is designed to work with. This module implements storage only; the
GET/PUT /repositories/{id}/env API endpoints are explicitly out of scope
for the current task.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class EnvironmentVariable(Base):
    __tablename__ = "environment_variables"
    __table_args__ = (
        # One current default value per key, per repository.
        Index(
            "uq_env_vars_repository_default_key",
            "repository_id",
            "key",
            unique=True,
            postgresql_where=text("deployment_id IS NULL"),
        ),
        # One override value per key, per deployment snapshot.
        Index(
            "uq_env_vars_deployment_override_key",
            "deployment_id",
            "key",
            unique=True,
            postgresql_where=text("deployment_id IS NOT NULL"),
        ),
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
    # NULL = repository-level default. Set = override snapshot tied to one deployment.
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    key: Mapped[str] = mapped_column(String(255), nullable=False)

    # Encrypted at rest — see app/utils/encryption.py. Never a plaintext column.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # Precomputed at write time (app/utils/encryption.py:is_sensitive_key)
    # so reads can decide whether to mask without touching value_encrypted.
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # EnvironmentVariable → Repository (N:1) — always set.
    repository: Mapped["Repository"] = relationship(  # noqa: F821
        "Repository", back_populates="environment_variables"
    )

    # EnvironmentVariable → Deployment (N:1) — only set for override rows.
    deployment: Mapped["Deployment | None"] = relationship(  # noqa: F821
        "Deployment", back_populates="env_var_overrides"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        # Deliberately excludes value_encrypted from repr — never print
        # even the ciphertext casually, let alone plaintext.
        kind = "override" if self.deployment_id else "default"
        return f"<EnvironmentVariable id={self.id} key={self.key!r} kind={kind}>"
