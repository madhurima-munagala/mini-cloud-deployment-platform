"""
Alembic environment script.

Deliberately reads the connection string from Settings (i.e. from the
DATABASE_URL environment variable) rather than from alembic.ini, so the
database URL is never hardcoded or committed.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import Base + all models so `target_metadata` reflects all 7 tables —
# required for autogenerate to see anything, and for --sql/offline mode
# to render the full schema.
from app.core.config import settings
from app.core.database import Base
from app.models import (  # noqa: F401
    Deployment,
    DeploymentLog,
    EnvironmentVariable,
    MonitoringMetric,
    Repository,
    Session,
    User,
)

config = context.config

# Inject the real DB URL at runtime instead of reading it from alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without a live DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connects to the database directly)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
