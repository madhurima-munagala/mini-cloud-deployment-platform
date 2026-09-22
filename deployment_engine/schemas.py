from pydantic import BaseModel, Field


class RepositoryInfo(BaseModel):
    clone_url: str
    branch: str


class EnvironmentVariable(BaseModel):
    key: str
    value: str


class DeploymentRequest(BaseModel):
    deployment_id: str
    repository: RepositoryInfo
    env_vars: list[EnvironmentVariable] = Field(default_factory=list)
    callback_base_url: str