"""
Response schema for GET /repositories.

Exposes only the fields listed in the Stage 4 contract — no GitHub
token, no encrypted token, no internal secret is a field on this model
at all, so it's structurally impossible for this schema to leak one.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_repo_id: int
    name: str
    full_name: str
    private: bool
    default_branch: str
    clone_url: str
    github_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime
