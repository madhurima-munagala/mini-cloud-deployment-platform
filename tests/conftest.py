"""
Shared fixtures for the database-layer test suite.

These tests intentionally run against a REAL PostgreSQL database, not
SQLite. The two things this schema most needs to verify — the partial
unique index on deployments and the two partial unique indexes on
environment_variables — are PostgreSQL-specific features
(`postgresql_where=...`). SQLite would silently create plain unique
indexes instead, which would pass or fail these tests for the wrong
reasons. See DATABASE_SETUP.md for how to point this suite at a test
database.

If TEST_DATABASE_URL / DATABASE_URL is not reachable, the whole suite is
skipped with a clear message rather than erroring — this lets the suite
still "run" (exit 0) in environments without PostgreSQL available (e.g.
CI dry-runs, or this sandboxed authoring environment), while making it
obvious that real verification still needs to happen against a live DB.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base
from app.models import (  # noqa: F401  (import registers all tables on Base.metadata)
    Deployment,
    DeploymentLog,
    EnvironmentVariable,
    MonitoringMetric,
    Repository,
    Session,
    User,
)


def _database_reachable(url: str) -> bool:
    try:
        probe_engine = create_engine(url)
        with probe_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe_engine.dispose()
        return True
    except OperationalError:
        return False


@pytest.fixture(scope="session")
def test_db_url() -> str:
    return settings.resolved_test_database_url


@pytest.fixture(scope="session")
def test_engine(test_db_url):
    if not _database_reachable(test_db_url):
        pytest.skip(
            f"Could not connect to test database at {test_db_url!r}. "
            "Set TEST_DATABASE_URL (or DATABASE_URL) to a reachable "
            "PostgreSQL instance before running the database test suite. "
            "See DATABASE_SETUP.md."
        )

    engine = create_engine(test_db_url, future=True)

    with engine.begin() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    Base.metadata.create_all(bind=engine)

    yield engine

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(test_engine) -> Generator[SASession, None, None]:
    """
    Wraps each test in an outer transaction that is always rolled back,
    so tests never leak data into each other and the test database stays
    empty between runs without needing a full drop/recreate per test.
    """
    connection = test_engine.connect()
    outer_transaction = connection.begin()

    SessionFactory = sessionmaker(bind=connection, future=True)
    session = SessionFactory()

    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------
# Small factory helpers shared across tests — keep test bodies focused on
# the behavior being verified, not on repetitive setup.
# ---------------------------------------------------------------------


def make_user(db_session: SASession, *, github_id: int | None = None) -> User:
    user = User(
        github_id=github_id if github_id is not None else uuid.uuid4().int >> 96,
        username="test-user",
        avatar_url=None,
        github_access_token_encrypted="encrypted-placeholder-token",
    )
    db_session.add(user)
    db_session.flush()
    return user


def make_repository(db_session: SASession, user: User, *, github_repo_id: int | None = None) -> Repository:
    repo = Repository(
        user_id=user.id,
        github_repo_id=github_repo_id if github_repo_id is not None else uuid.uuid4().int >> 96,
        name="sample-repo",
        full_name="test-user/sample-repo",
        private=False,
        default_branch="main",
        clone_url="https://github.com/test-user/sample-repo.git",
    )
    db_session.add(repo)
    db_session.flush()
    return repo


def make_deployment(db_session: SASession, repository: Repository, *, status: str = "pending", branch: str = "main") -> Deployment:
    deployment = Deployment(
        repository_id=repository.id,
        branch=branch,
        status=status,
    )
    db_session.add(deployment)
    db_session.flush()
    return deployment
