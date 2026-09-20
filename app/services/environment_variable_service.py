"""
Environment variable business logic.

Three distinct responsibilities, kept in one file since they all revolve
around the same `environment_variables` table (app/models/environment_variable.py)
and the same encryption utility (app/utils/encryption.py):

1. Repository-level DEFAULTS — full CRUD (list + full-replace), backing
   GET/PUT /repositories/{id}/env (app/api/v1/repositories.py).

2. Deployment-level OVERRIDES — write-only. There is no GET/PUT/DELETE
   endpoint for these; they are immutable snapshots created only as a
   side effect of POST /deployments (see build_override_rows(), used by
   app/services/deployment_service.py:create_deployment). This is
   deliberate, not an omission — matching the existing model's own
   docstring ("a snapshot of the value actually used for one specific
   deployment").

3. EFFECTIVE RESOLUTION for the engine payload — override wins over
   default for the same key; a default with no override is resolved
   LIVE from the repository at call time (not itself snapshotted, since
   nothing ever re-triggers a deployment later). Decrypts every value —
   this is the only function in the codebase that ever decrypts an
   environment_variables row, and it is called only from
   app/services/deployment_engine_client.py:run_engine_trigger,
   immediately before building the outbound engine payload.

Masking rule (Stage 8, stricter than earlier contract language): every
value returned by list_repository_defaults()/replace_repository_defaults()
is masked on the way out, regardless of is_sensitive — callers never see
plaintext from these two functions. is_sensitive is still computed and
stored (via is_sensitive_key()) because it's part of the stored row and
may be useful later; it no longer gates masking.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.deployment import Deployment
from app.models.environment_variable import EnvironmentVariable
from app.utils.encryption import decrypt_value, encrypt_value, is_sensitive_key

_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class InvalidEnvVarError(Exception):
    """
    Raised by validate_env_vars() when one or more keys are malformed
    (don't match _KEY_PATTERN) or repeated within the same payload.
    `invalid_keys` holds every offending key (both categories combined,
    de-duplicated) so the caller can report all of them at once rather
    than failing on the first.
    """

    def __init__(self, invalid_keys: list[str]):
        self.invalid_keys = invalid_keys
        super().__init__(f"Invalid environment variable keys: {invalid_keys}")


def validate_env_vars(env_vars: list[dict]) -> None:
    """
    Validates a list of {"key": ..., "value": ...} dicts:
    - every key must match ^[A-Z][A-Z0-9_]*$
    - no key may repeat within this list

    Pure input validation — touches no database, encrypts nothing.
    Callers (both PUT /repositories/{id}/env and POST /deployments) must
    call this BEFORE any write, so a validation failure never leaves a
    partial write behind.
    """
    malformed: list[str] = []
    seen: set[str] = set()
    duplicated: set[str] = set()

    for item in env_vars:
        key = item["key"]
        if not _KEY_PATTERN.match(key):
            malformed.append(key)
        if key in seen:
            duplicated.add(key)
        seen.add(key)

    invalid = sorted(set(malformed) | duplicated)
    if invalid:
        raise InvalidEnvVarError(invalid)


# --- repository-level defaults (deployment_id IS NULL) ---


def list_repository_defaults(db: DBSession, repository_id: uuid.UUID) -> list[EnvironmentVariable]:
    """Returns the repository's default rows, ordered by key for determinism."""
    return list(
        db.execute(
            select(EnvironmentVariable)
            .where(
                EnvironmentVariable.repository_id == repository_id,
                EnvironmentVariable.deployment_id.is_(None),
            )
            .order_by(EnvironmentVariable.key.asc())
        ).scalars()
    )


def replace_repository_defaults(
    db: DBSession, repository_id: uuid.UUID, env_vars: list[dict]
) -> list[EnvironmentVariable]:
    """
    Full-replace semantics: deletes every existing default row for this
    repository, then inserts the new set. Caller must have already
    called validate_env_vars() on `env_vars`. Commits once.
    """
    existing = db.execute(
        select(EnvironmentVariable).where(
            EnvironmentVariable.repository_id == repository_id,
            EnvironmentVariable.deployment_id.is_(None),
        )
    ).scalars().all()
    for row in existing:
        db.delete(row)
    # SQLAlchemy's unit of work emits INSERTs before DELETEs within a single
    # flush, so replacing a key with itself would hit
    # uq_env_vars_repository_default_key while the old row still exists.
    # Flush the DELETEs first (same transaction; the single commit below is
    # unchanged, so the replace is still atomic).
    db.flush()

    new_rows = [
        EnvironmentVariable(
            repository_id=repository_id,
            deployment_id=None,
            key=item["key"],
            value_encrypted=encrypt_value(item["value"]),
            is_sensitive=is_sensitive_key(item["key"]),
        )
        for item in env_vars
    ]
    db.add_all(new_rows)
    db.commit()

    for row in new_rows:
        db.refresh(row)
    return new_rows


# --- deployment-level overrides (write-only snapshot, no CRUD endpoints) ---


def build_override_rows(
    repository_id: uuid.UUID, deployment_id: uuid.UUID, env_vars: list[dict]
) -> list[EnvironmentVariable]:
    """
    Builds (but does NOT add/commit) EnvironmentVariable override rows
    for one deployment. The caller —
    app/services/deployment_service.py:create_deployment — is
    responsible for db.add_all()-ing these in the SAME transaction as
    the deployment row itself and committing once, so the deployment and
    its override snapshot persist atomically together or not at all.
    Caller must have already called validate_env_vars() on `env_vars`.
    """
    return [
        EnvironmentVariable(
            repository_id=repository_id,
            deployment_id=deployment_id,
            key=item["key"],
            value_encrypted=encrypt_value(item["value"]),
            is_sensitive=is_sensitive_key(item["key"]),
        )
        for item in env_vars
    ]


# --- effective resolution for the engine payload (decrypts) ---


def resolve_effective_env_vars(db: DBSession, deployment: Deployment) -> list[dict]:
    """
    Resolves the effective {"key", "value"} list for one deployment:
    a deployment-level override wins over a repository-level default for
    the same key; a key with no override falls back to the CURRENT
    repository default (looked up live here, not snapshotted — nothing
    ever re-triggers a deployment later, so "live at trigger time" and
    "frozen" are equivalent in practice for defaults).

    This is the ONLY function that ever calls decrypt_value() on an
    environment_variables row. Called only from
    app/services/deployment_engine_client.py:run_engine_trigger,
    immediately before building the outbound engine payload — the return
    value must never be logged, stored, or returned from an API response.
    """
    defaults = db.execute(
        select(EnvironmentVariable).where(
            EnvironmentVariable.repository_id == deployment.repository_id,
            EnvironmentVariable.deployment_id.is_(None),
        )
    ).scalars().all()
    overrides = db.execute(
        select(EnvironmentVariable).where(
            EnvironmentVariable.deployment_id == deployment.id,
        )
    ).scalars().all()

    effective: dict[str, EnvironmentVariable] = {row.key: row for row in defaults}
    for row in overrides:
        effective[row.key] = row  # override wins over default for the same key

    return [
        {"key": row.key, "value": decrypt_value(row.value_encrypted)}
        for row in sorted(effective.values(), key=lambda r: r.key)
    ]
