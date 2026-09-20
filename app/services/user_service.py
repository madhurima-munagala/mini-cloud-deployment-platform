"""
User lookup/creation/update, keyed by GitHub's numeric ID
(users.github_id — the stable identifier per DATABASE_DESIGN.md §2.1).

Uses the EXISTING app.models.user.User model as-is — no schema changes.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.user import User


def get_or_create_user(
    db: DBSession,
    *,
    github_id: int,
    username: str,
    avatar_url: str | None,
    github_access_token_encrypted: str,
) -> User:
    """
    Looks up a user by github_id. Creates one if not found; otherwise
    updates the existing row's profile fields and encrypted token in
    place. Never creates a duplicate for the same github_id.
    """
    user = db.execute(select(User).where(User.github_id == github_id)).scalar_one_or_none()

    if user is None:
        user = User(
            github_id=github_id,
            username=username,
            avatar_url=avatar_url,
            github_access_token_encrypted=github_access_token_encrypted,
        )
        db.add(user)
    else:
        user.username = username
        user.avatar_url = avatar_url
        user.github_access_token_encrypted = github_access_token_encrypted

    db.commit()
    db.refresh(user)
    return user
