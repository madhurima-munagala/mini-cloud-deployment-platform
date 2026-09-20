"""
Response schema for GET /auth/me.

Deliberately does NOT include github_access_token_encrypted or any
session/token field — this schema is structurally incapable of leaking
either, regardless of what a route handler does with it, since there's
no field to accidentally serialize.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_id: int
    username: str
    avatar_url: str | None
    created_at: datetime
