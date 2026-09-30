"""Engine and session factory.

This is the one place the storage backend is named. `CITADEL_DATABASE_URL`
selects it: SQLite by default (design doc §8 permits it), Postgres by setting
the variable. No other module constructs an engine.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import config

_connect_args = {}
if config.DATABASE_URL.startswith("sqlite"):
    # The Observability writer serializes its own writes with a lock but runs
    # on the FastAPI thread pool, so the connection must be shareable.
    _connect_args["check_same_thread"] = False

engine: Engine = create_engine(
    config.DATABASE_URL,
    echo=config.SQL_ECHO,
    future=True,
    connect_args=_connect_args,
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover
    """Foreign keys are off by default in SQLite; the schema's integrity
    constraints are load-bearing here, so turn them on."""
    if config.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception.

    The approval decision (§6.10) needs Approval + Artifact + Task to move in
    *one* transaction; this is the primitive that guarantees it.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
