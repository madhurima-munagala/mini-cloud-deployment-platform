"""initial schema: users, sessions, repositories, deployments,
environment_variables, deployment_logs, monitoring_metrics

Implements DATABASE_DESIGN.md v1.0 in full, including:
- the partial unique index enforcing "no two active deployments per
  repository simultaneously" (API_CONTRACT.md §4.1)
- the two partial unique indexes on environment_variables distinguishing
  repository-level defaults from per-deployment override snapshots
- CHECK constraints for deployments.status, deployment_logs.level, and
  monitoring_metrics.container_status
- ON DELETE CASCADE on every foreign key, per the design's delete policy

Revision ID: 0001
Revises:
Create Date: 2026-09-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() is built into PostgreSQL core since v13, but we
    # enable pgcrypto defensively so this migration also works unmodified
    # against older PostgreSQL versions where the function lives there.
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("github_access_token_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
    )
    op.create_index("ix_users_github_id", "users", ["github_id"])

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("session_token_hash", name="uq_sessions_session_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # ------------------------------------------------------------------
    # repositories
    # ------------------------------------------------------------------
    op.create_table(
        "repositories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=500), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "default_branch",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'main'"),
        ),
        sa.Column("clone_url", sa.Text(), nullable=False),
        sa.Column("github_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_repositories_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "user_id", "github_repo_id", name="uq_repositories_user_id_github_repo_id"
        ),
    )
    op.create_index("ix_repositories_user_id", "repositories", ["user_id"])

    # ------------------------------------------------------------------
    # deployments
    # ------------------------------------------------------------------
    op.create_table(
        "deployments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("live_url", sa.Text(), nullable=True),
        sa.Column("container_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name="fk_deployments_repository_id_repositories",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending','building','starting','running','failed','stopped')",
            name="ck_deployments_status",
        ),
    )
    op.create_index("ix_deployments_repository_id", "deployments", ["repository_id"])
    op.create_index(
        "ix_deployments_repository_id_created_at", "deployments", ["repository_id", "created_at"]
    )

    # CRITICAL: partial unique index enforcing "no two active deployments
    # for the same repository simultaneously" (API_CONTRACT.md §4.1).
    # Only rows with status pending/building/starting participate in the
    # uniqueness check — running/failed/stopped rows are unrestricted,
    # which is what allows deployment history and the redeploy/replacement
    # flow (§9.6) to work.
    op.create_index(
        "uq_deployments_active_per_repository",
        "deployments",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'building', 'starting')"),
    )

    # ------------------------------------------------------------------
    # environment_variables
    # ------------------------------------------------------------------
    op.create_table(
        "environment_variables",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "is_sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name="fk_environment_variables_repository_id_repositories",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name="fk_environment_variables_deployment_id_deployments",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_environment_variables_repository_id", "environment_variables", ["repository_id"]
    )
    op.create_index(
        "ix_environment_variables_deployment_id", "environment_variables", ["deployment_id"]
    )

    # One current default value per key, per repository.
    op.create_index(
        "uq_env_vars_repository_default_key",
        "environment_variables",
        ["repository_id", "key"],
        unique=True,
        postgresql_where=sa.text("deployment_id IS NULL"),
    )
    # One override value per key, per deployment snapshot.
    op.create_index(
        "uq_env_vars_deployment_override_key",
        "environment_variables",
        ["deployment_id", "key"],
        unique=True,
        postgresql_where=sa.text("deployment_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # deployment_logs (BIGSERIAL PK — append-only, high volume)
    # ------------------------------------------------------------------
    op.create_table(
        "deployment_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name="fk_deployment_logs_deployment_id_deployments",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("level IN ('info','warn','error')", name="ck_deployment_logs_level"),
    )
    op.create_index(
        "ix_deployment_logs_deployment_id_timestamp",
        "deployment_logs",
        ["deployment_id", "timestamp"],
    )

    # ------------------------------------------------------------------
    # monitoring_metrics (BIGSERIAL PK — append-only, high volume)
    # ------------------------------------------------------------------
    op.create_table(
        "monitoring_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("container_status", sa.String(length=10), nullable=False),
        sa.Column("cpu_percent", sa.Numeric(6, 2), nullable=False),
        sa.Column("memory_mb", sa.Numeric(10, 2), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name="fk_monitoring_metrics_deployment_id_deployments",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "container_status IN ('running','stopped','crashed','unknown')",
            name="ck_monitoring_metrics_container_status",
        ),
    )
    op.create_index(
        "ix_monitoring_metrics_deployment_id_timestamp",
        "monitoring_metrics",
        ["deployment_id", "timestamp"],
    )


def downgrade() -> None:
    # Drop in strict reverse dependency order.
    op.drop_index("ix_monitoring_metrics_deployment_id_timestamp", table_name="monitoring_metrics")
    op.drop_table("monitoring_metrics")

    op.drop_index("ix_deployment_logs_deployment_id_timestamp", table_name="deployment_logs")
    op.drop_table("deployment_logs")

    op.drop_index("uq_env_vars_deployment_override_key", table_name="environment_variables")
    op.drop_index("uq_env_vars_repository_default_key", table_name="environment_variables")
    op.drop_index("ix_environment_variables_deployment_id", table_name="environment_variables")
    op.drop_index("ix_environment_variables_repository_id", table_name="environment_variables")
    op.drop_table("environment_variables")

    op.drop_index("uq_deployments_active_per_repository", table_name="deployments")
    op.drop_index("ix_deployments_repository_id_created_at", table_name="deployments")
    op.drop_index("ix_deployments_repository_id", table_name="deployments")
    op.drop_table("deployments")

    op.drop_index("ix_repositories_user_id", table_name="repositories")
    op.drop_table("repositories")

    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_users_github_id", table_name="users")
    op.drop_table("users")

    # Not dropping the pgcrypto extension — it may be relied on by other
    # objects in a shared database; leaving extensions in place on
    # downgrade is the safer default.
