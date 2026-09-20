"""
SQLAlchemy database foundation.

This module owns:
- the SQLAlchemy engine (PostgreSQL connection, built from Settings — no
  hardcoded credentials)
- the session factory
- the declarative Base that every model in app/models/ inherits from
- get_db(), a plain generator dependency that FastAPI's Depends() can use
  once the API layer is built (this file does not import FastAPI itself,
  keeping the database layer framework-agnostic for now)
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,  # avoids using dead connections after DB restarts/idle timeouts
    echo=settings.sql_echo,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Declarative base shared by all 7 models in app/models/."""

    pass


def get_db() -> Generator[Session, None, None]:
    """
    Request-scoped database session.

    Usage (once the API layer exists):
        @app.get("/some-endpoint")
        def handler(db: Session = Depends(get_db)):
            ...

    Always closes the session, even if the request raises.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
