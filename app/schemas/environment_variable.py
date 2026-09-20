"""
Request/response schemas for:
- GET/PUT /repositories/{id}/env (repository-level defaults — full CRUD)
- the optional `env_vars` field on POST /deployments (deployment-level
  override snapshot — write-only; see
  app/services/environment_variable_service.py for why there's no
  corresponding read/update/delete endpoint)

Every read response masks every value, including non-sensitive keys —
Stage 8's explicit rule, stricter than earlier contract language. No
schema here ever carries a plaintext value out of the API.
"""

from pydantic import BaseModel


class EnvVarIn(BaseModel):
    key: str
    value: str


class EnvVarSetRequest(BaseModel):
    env_vars: list[EnvVarIn]


class EnvVarOut(BaseModel):
    key: str
    # Always the fixed mask (app.utils.encryption.mask_value(True)),
    # regardless of the underlying is_sensitive flag — see this file's
    # module docstring.
    value: str
