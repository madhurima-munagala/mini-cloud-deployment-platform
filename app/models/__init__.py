"""
Importing this package registers all 7 tables on Base.metadata.

Both Alembic's env.py and the test suite import `app.models` (not the
individual model modules) specifically so that nothing is silently left
out of autogenerate/create_all — a new model file only takes effect once
it's added to the imports below.
"""

from app.models.deployment import Deployment  # noqa: F401
from app.models.deployment_log import DeploymentLog  # noqa: F401
from app.models.environment_variable import EnvironmentVariable  # noqa: F401
from app.models.monitoring_metric import MonitoringMetric  # noqa: F401
from app.models.repository import Repository  # noqa: F401
from app.models.session import Session  # noqa: F401
from app.models.user import User  # noqa: F401

__all__ = [
    "User",
    "Session",
    "Repository",
    "Deployment",
    "EnvironmentVariable",
    "DeploymentLog",
    "MonitoringMetric",
]
