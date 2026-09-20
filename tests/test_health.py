"""
Tests for GET /api/v1/health — the only endpoint added in Stage 2.

Two cases:
1. Happy path — requires a real, reachable PostgreSQL database (via the
   existing get_db() -> real engine), same requirement as the
   database-layer suite. See DATABASE_SETUP.md.
2. Simulated DB failure — does NOT require a real database. It overrides
   the get_db() dependency with a fake session whose .execute() raises
   OperationalError, and asserts the response still follows the shared
   error shape from API_CONTRACT.md §1 instead of crashing with a raw 500.
"""

from sqlalchemy.exc import OperationalError

from app.core.database import get_db
from app.main import app
from tests.api_conftest import client  # noqa: F401  (fixture import)


def test_health_check_returns_ok_when_database_is_reachable(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"data": {"status": "ok", "database": "connected"}}


def test_health_check_returns_shared_error_shape_when_database_is_unreachable(client):
    class _BrokenSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        def close(self):
            pass

    def _broken_get_db():
        yield _BrokenSession()

    app.dependency_overrides[get_db] = _broken_get_db
    try:
        response = client.get("/api/v1/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "message" in body["error"]
    assert body["error"]["details"] is None
