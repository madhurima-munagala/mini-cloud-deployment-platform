"""
Synchronizes GitHub repository data (already fetched via
github_repository_service) into the local `repositories` table.

Uses the EXISTING Repository model and its (user_id, github_repo_id)
unique constraint as-is — no schema changes. Updates a matching row if
found, inserts otherwise — never creates a duplicate for the same user
+ GitHub repo.

This function does NOT catch exceptions itself. A failure here (e.g. a
DB error) is left to propagate to the caller
(app/api/v1/repositories.py), which is responsible for rolling back the
session and letting the error surface as a proper response — see that
module for the rollback-and-propagate behavior.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.repository import Repository
from app.models.user import User


def sync_repositories(
    db: DBSession, user: User, github_repos: list[dict]
) -> list[Repository]:
    """
    For each dict in github_repos (shape produced by
    github_repository_service.list_repositories_for_user), finds the
    existing Repository row for (user.id, github_repo_id) and updates
    it in place, or creates a new one. Commits once for the whole batch.
    """
    existing_by_github_id = {
        repo.github_repo_id: repo
        for repo in db.execute(
            select(Repository).where(Repository.user_id == user.id)
        ).scalars()
    }

    synced: list[Repository] = []

    for github_repo in github_repos:
        github_repo_id = github_repo["github_repo_id"]
        repo = existing_by_github_id.get(github_repo_id)

        if repo is None:
            repo = Repository(
                user_id=user.id,
                github_repo_id=github_repo_id,
                name=github_repo["name"],
                full_name=github_repo["full_name"],
                private=github_repo["private"],
                default_branch=github_repo["default_branch"],
                clone_url=github_repo["clone_url"],
                github_updated_at=github_repo["github_updated_at"],
            )
            db.add(repo)
        else:
            repo.name = github_repo["name"]
            repo.full_name = github_repo["full_name"]
            repo.private = github_repo["private"]
            repo.default_branch = github_repo["default_branch"]
            repo.clone_url = github_repo["clone_url"]
            repo.github_updated_at = github_repo["github_updated_at"]

        synced.append(repo)

    db.commit()

    for repo in synced:
        db.refresh(repo)

    return synced
