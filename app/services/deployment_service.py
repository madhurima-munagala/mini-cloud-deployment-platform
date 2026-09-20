"""
Deployment business logic: creation (with ownership + concurrency
checks), listing, detail lookup, and read-only log listing.

Ownership is always enforced by joining through Repository.user_id —
never by trusting a request parameter — mirroring
app/services/repository_service.py's approach.

No Docker/EC2/deployment-engine integration happens here — this stage
only persists/reads rows in the existing `deployments` and
`deployment_logs` tables. Nothing here writes to `deployment_logs` on
the happy path; until engine integration exists, GET
/deployments/{id}/logs correctly returns an empty list. (One narrow
exception, added in Stage 7: app/services/deployment_callback_service.py
writes a single error log line specifically when the backend itself
can't reach the engine at all — see that module.)

Stage 7: list_deployments/list_deployment_logs both add `id` as a
secondary ORDER BY key (alongside created_at / timestamp respectively)
for stable, deterministic pagination when multiple rows share the same
primary sort value. list_deployment_logs's `since` filter is inclusive
(>=) rather than exclusive (>) for the same reason — see that function's
docstring.

Stage 8: create_deployment() optionally accepts `env_vars` — a
deployment-level override snapshot. The deployment row and its override
rows are written in ONE transaction (db.add() both, single db.commit())
so they persist atomically: either both succeed or neither does. Row
construction/encryption itself lives in
app/services/environment_variable_service.py:build_override_rows();
this module only orchestrates the atomic write.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import contains_eager

from app.models.deployment import ACTIVE_DEPLOYMENT_STATUSES, Deployment
from app.models.deployment_log import DeploymentLog
from app.models.repository import Repository
from app.models.user import User
from app.services import environment_variable_service


class RepositoryNotFoundError(Exception):
    """Raised when repository_id doesn't exist or isn't owned by the user."""


class DeploymentInProgressError(Exception):
    """Raised when the repository already has an active (pending/building/
    starting) deployment — API_CONTRACT.md's concurrency rule.

    `existing_deployment_id` is the active deployment that caused the
    conflict, surfaced by the API as details.existing_deployment_id
    (API_CONTRACT.md §4.1). It is None only if that deployment could not
    be re-identified after a lost race (it finished between the conflict
    and the follow-up lookup).
    """

    def __init__(self, existing_deployment_id: uuid.UUID | None = None):
        self.existing_deployment_id = existing_deployment_id
        super().__init__(f"Active deployment already exists: {existing_deployment_id}")


def _get_owned_repository(db: DBSession, user: User, repository_id: uuid.UUID) -> Repository:
    repo = db.execute(
        select(Repository).where(
            Repository.id == repository_id, Repository.user_id == user.id
        )
    ).scalar_one_or_none()
    if repo is None:
        raise RepositoryNotFoundError()
    return repo


def _find_active_deployment_id(db: DBSession, repository_id: uuid.UUID) -> uuid.UUID | None:
    return db.execute(
        select(Deployment.id).where(
            Deployment.repository_id == repository_id,
            Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
        )
    ).scalars().first()


def create_deployment(
    db: DBSession,
    user: User,
    *,
    repository_id: uuid.UUID,
    branch: str | None,
    env_vars: list[dict] | None = None,
) -> Deployment:
    """
    `env_vars`, if provided, must already be validated by the caller
    (environment_variable_service.validate_env_vars()) — this function
    assumes it's clean and focuses on the atomic write.
    """
    repo = _get_owned_repository(db, user, repository_id)

    existing_active = db.execute(
        select(Deployment).where(
            Deployment.repository_id == repo.id,
            Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
        )
    ).scalar_one_or_none()
    if existing_active is not None:
        raise DeploymentInProgressError(existing_active.id)

    deployment = Deployment(
        repository_id=repo.id,
        branch=branch or repo.default_branch,
        status="pending",
    )
    db.add(deployment)
    # Captured now: a rollback below expires every loaded instance.
    repository_pk = repo.id

    if env_vars:
        # Flush (not commit) so deployment.id is populated without
        # ending the transaction — the override rows below join the
        # SAME pending transaction as the deployment row, so both commit
        # together or neither does (atomicity requirement).
        db.flush()
        override_rows = environment_variable_service.build_override_rows(
            repo.id, deployment.id, env_vars
        )
        db.add_all(override_rows)

    try:
        db.commit()
    except IntegrityError:
        # Belt-and-suspenders: the pre-check above handles the common
        # case with a clean error; if two requests raced past it, the
        # database's own partial unique index
        # (app/models/deployment.py: uq_deployments_active_per_repository)
        # is the real guarantee. Caught here and converted to the same
        # domain error rather than propagating as an unhandled 500.
        # (env_vars is assumed pre-validated by the caller, so an
        # IntegrityError from the override rows' own unique index should
        # not realistically occur here — but if it somehow did, rolling
        # back and re-raising this same domain error is still the safe,
        # atomic outcome: nothing partially persists either way.)
        db.rollback()
        raise DeploymentInProgressError(_find_active_deployment_id(db, repository_pk))

    db.refresh(deployment)
    return deployment


def list_deployments(
    db: DBSession,
    user: User,
    *,
    repository_id: uuid.UUID | None,
    status: str | None,
    page: int,
    per_page: int,
) -> tuple[list[Deployment], bool]:
    """
    Returns (deployments for this page, has_next). One extra row is
    fetched beyond `per_page` purely to learn whether a next page exists
    (API_CONTRACT.md §4.2 meta.has_next); it is never returned.
    """
    query = (
        select(Deployment)
        .join(Repository, Deployment.repository_id == Repository.id)
        .where(Repository.user_id == user.id)
        # The join above already fetches the repository row; load it into
        # Deployment.repository from that same query so building each
        # item's repository_name doesn't cost one extra query per row.
        .options(contains_eager(Deployment.repository))
        .order_by(Deployment.created_at.desc(), Deployment.id.asc())
    )
    if repository_id is not None:
        query = query.where(Deployment.repository_id == repository_id)
    if status is not None:
        query = query.where(Deployment.status == status)

    query = query.offset((page - 1) * per_page).limit(per_page + 1)

    rows = list(db.execute(query).scalars().all())
    return rows[:per_page], len(rows) > per_page


def get_deployment(db: DBSession, user: User, deployment_id: uuid.UUID) -> Deployment | None:
    """
    Returns the deployment only if it exists AND belongs (via its
    repository) to `user` — otherwise None, which callers map to 404,
    never 403, so a deployment ID's existence is never confirmed to
    someone who doesn't own it.
    """
    return db.execute(
        select(Deployment)
        .join(Repository, Deployment.repository_id == Repository.id)
        .options(contains_eager(Deployment.repository))
        .where(Deployment.id == deployment_id, Repository.user_id == user.id)
    ).scalar_one_or_none()


def list_deployment_logs(
    db: DBSession,
    user: User,
    deployment_id: uuid.UUID,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> list[DeploymentLog] | None:
    """
    Returns None if the deployment doesn't exist or isn't owned by user
    (caller maps that to 404 DEPLOYMENT_NOT_FOUND) — otherwise a list,
    which may legitimately be empty (no producer writes logs yet).

    `since` is inclusive: rows with timestamp == since are included, not
    just timestamp > since. See the inline comment below for why.
    """
    deployment = get_deployment(db, user, deployment_id)
    if deployment is None:
        return None

    query = (
        select(DeploymentLog)
        .where(DeploymentLog.deployment_id == deployment_id)
        .order_by(DeploymentLog.timestamp.asc(), DeploymentLog.id.asc())
    )
    if since is not None:
        # Inclusive (>=), not exclusive (>): if multiple log lines share
        # the exact timestamp a client last saw (realistic, since the
        # engine sends logs in batches — nothing guarantees distinct
        # timestamps within one batch), an exclusive filter would drop
        # any tied line forever. Inclusive means a boundary line can
        # reappear on the next poll; DeploymentLogOut.id (added this
        # stage) lets a client de-duplicate that reappearance.
        query = query.where(DeploymentLog.timestamp >= since)
    query = query.limit(limit)

    return list(db.execute(query).scalars().all())
