"""
Database-layer tests only — no GitHub, Docker, EC2, or frontend behavior
is exercised here. Each test corresponds directly to one item in the
implementation task's test list.

Run with (see DATABASE_SETUP.md for full setup):
    pytest
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Deployment, DeploymentLog, EnvironmentVariable, MonitoringMetric, Session
from app.utils.encryption import decrypt_value, encrypt_value, is_sensitive_key
from tests.conftest import make_deployment, make_repository, make_user


# 1. Users can be created.
def test_user_can_be_created(db_session):
    user = make_user(db_session)

    assert user.id is not None
    assert user.username == "test-user"
    assert user.created_at is not None
    assert user.updated_at is not None
    # Sensitive token field exists but is opaque/encrypted — not asserting
    # its literal value here, just that it round-trips as stored.
    assert user.github_access_token_encrypted == "encrypted-placeholder-token"


# 2. Sessions correctly reference users.
def test_session_references_user(db_session):
    user = make_user(db_session)

    session = Session(
        user_id=user.id,
        session_token_hash="hash-of-some-cookie-value",
        expires_at=datetime.now(timezone.utc),
    )
    db_session.add(session)
    db_session.flush()

    assert session.user_id == user.id
    assert session.user.id == user.id
    assert session in user.sessions


# 3. Repositories correctly reference users.
def test_repository_references_user(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    assert repo.user_id == user.id
    assert repo.user.id == user.id
    assert repo in user.repositories


# 4. Duplicate (user_id, github_repo_id) repositories are rejected.
def test_duplicate_user_github_repo_id_rejected(db_session):
    user = make_user(db_session)
    make_repository(db_session, user, github_repo_id=555111)

    with pytest.raises(IntegrityError):
        make_repository(db_session, user, github_repo_id=555111)

    db_session.rollback()


# 5. Deployments correctly reference repositories.
def test_deployment_references_repository(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)
    deployment = make_deployment(db_session, repo)

    assert deployment.repository_id == repo.id
    assert deployment.repository.id == repo.id
    assert deployment in repo.deployments


# 6. Invalid deployment statuses are rejected.
def test_invalid_deployment_status_rejected(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    with pytest.raises(IntegrityError):
        make_deployment(db_session, repo, status="not-a-real-status")

    db_session.rollback()


# 7. The partial unique deployment constraint prevents two active
#    deployments for the same repository.
def test_partial_unique_index_blocks_two_active_deployments(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    make_deployment(db_session, repo, status="pending")

    with pytest.raises(IntegrityError):
        make_deployment(db_session, repo, status="building")

    db_session.rollback()


# 8. A running deployment does NOT block another deployment, per the
#    locked contract (only pending/building/starting are "active").
def test_running_deployment_does_not_block_new_deployment(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    running = make_deployment(db_session, repo, status="running")
    new_pending = make_deployment(db_session, repo, status="pending")

    assert running.status == "running"
    assert new_pending.status == "pending"
    assert running.id != new_pending.id


# 9. Environment-variable default uniqueness works.
def test_environment_variable_default_uniqueness(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    first = EnvironmentVariable(
        repository_id=repo.id,
        deployment_id=None,
        key="PORT",
        value_encrypted=encrypt_value("3000"),
        is_sensitive=is_sensitive_key("PORT"),
    )
    db_session.add(first)
    db_session.flush()

    duplicate = EnvironmentVariable(
        repository_id=repo.id,
        deployment_id=None,
        key="PORT",
        value_encrypted=encrypt_value("4000"),
        is_sensitive=is_sensitive_key("PORT"),
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


# 10. Deployment override uniqueness works.
def test_environment_variable_deployment_override_uniqueness(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)
    deployment = make_deployment(db_session, repo, status="pending")

    first_override = EnvironmentVariable(
        repository_id=repo.id,
        deployment_id=deployment.id,
        key="API_KEY",
        value_encrypted=encrypt_value("sk_live_123"),
        is_sensitive=is_sensitive_key("API_KEY"),
    )
    db_session.add(first_override)
    db_session.flush()

    duplicate_override = EnvironmentVariable(
        repository_id=repo.id,
        deployment_id=deployment.id,
        key="API_KEY",
        value_encrypted=encrypt_value("sk_live_456"),
        is_sensitive=is_sensitive_key("API_KEY"),
    )
    db_session.add(duplicate_override)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


# 11. Logs correctly reference deployments.
def test_logs_reference_deployment(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)
    deployment = make_deployment(db_session, repo, status="pending")

    log = DeploymentLog(
        deployment_id=deployment.id,
        timestamp=datetime.now(timezone.utc),
        level="info",
        message="Cloning repository...",
    )
    db_session.add(log)
    db_session.flush()

    assert log.deployment_id == deployment.id
    assert log.deployment.id == deployment.id
    assert log in deployment.logs


# 12. Metrics correctly reference deployments.
def test_metrics_reference_deployment(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)
    deployment = make_deployment(db_session, repo, status="running")

    metric = MonitoringMetric(
        deployment_id=deployment.id,
        container_status="running",
        cpu_percent=Decimal("12.40"),
        memory_mb=Decimal("184.20"),
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add(metric)
    db_session.flush()

    assert metric.deployment_id == deployment.id
    assert metric.deployment.id == deployment.id
    assert metric in deployment.metrics


# 13. Environment-variable encryption/decryption works.
def test_encryption_round_trip():
    plaintext = "sk_live_super_secret_value"

    ciphertext = encrypt_value(plaintext)

    assert ciphertext != plaintext
    assert decrypt_value(ciphertext) == plaintext


# 14. Plaintext environment-variable values are not stored in the
#     database representation.
def test_plaintext_value_never_stored(db_session):
    user = make_user(db_session)
    repo = make_repository(db_session, user)

    plaintext = "sk_live_should_never_appear_in_storage"
    env_var = EnvironmentVariable(
        repository_id=repo.id,
        deployment_id=None,
        key="API_KEY",
        value_encrypted=encrypt_value(plaintext),
        is_sensitive=is_sensitive_key("API_KEY"),
    )
    db_session.add(env_var)
    db_session.flush()
    db_session.refresh(env_var)

    # The column that actually persists to PostgreSQL must never equal,
    # or even contain, the plaintext value.
    assert env_var.value_encrypted != plaintext
    assert plaintext not in env_var.value_encrypted

    # But it must still be recoverable by the one legitimate code path
    # (the encryption utility), proving this isn't just corrupted data.
    assert decrypt_value(env_var.value_encrypted) == plaintext
