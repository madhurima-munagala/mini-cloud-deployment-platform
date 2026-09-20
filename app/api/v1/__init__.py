"""
Aggregates every v1 router into a single APIRouter, which app/main.py
mounts at settings.api_v1_prefix (/api/v1 by default).

This is the one file that needs to change to register a new group of
endpoints. Only the health router exists in this stage — later stages
add auth, repositories, deployments, environment variable, and
monitoring routers here in the same pattern:

    from app.api.v1.auth import router as auth_router
    api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.deployments import router as deployments_router
from app.api.v1.health import router as health_router
from app.api.v1.repositories import router as repositories_router

api_router = APIRouter()

api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(repositories_router, prefix="/repositories", tags=["repositories"])
api_router.include_router(deployments_router, prefix="/deployments", tags=["deployments"])
