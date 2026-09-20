"""
GET /api/v1/health

No authentication required. Confirms two things at once:
1. The FastAPI process is up and routing correctly.
2. It can actually reach PostgreSQL through the existing get_db()
   dependency from app/core/database.py — not just that the process is
   alive.

This is the only business endpoint added in Stage 2.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import error_response

router = APIRouter()


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except OperationalError:
        return error_response(
            code="DATABASE_UNAVAILABLE",
            message="The database is currently unreachable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return {"data": {"status": "ok", "database": "connected"}}
