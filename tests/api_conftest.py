"""
Fixtures for testing the FastAPI app itself (routing, error-handling
shape, health check).

Deliberately kept in a separate file from tests/conftest.py, which
provides the existing database-layer fixtures (test_engine, db_session,
factory helpers) and is not modified by this stage. pytest auto-discovers
both files; nothing here overrides or depends on the other file's
fixtures.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
