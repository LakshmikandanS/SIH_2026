"""Shared fixtures.

The database URL is set here, before any `app` module is imported, so the
suite never touches the developer's working database.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

# One database per pytest *process*, not one per machine. Two concurrent runs
# (two agents building different steps, or `pytest -p xdist`) otherwise share a
# single SQLite file and corrupt each other's schema mid-run -- which surfaces
# as `OperationalError` in whichever suite happens to lose the race, in tests
# that are individually fine. The pid makes the collision impossible.
_TEST_DB = Path(tempfile.gettempdir()) / f"citadel_test_{os.getpid()}.db"
os.environ.setdefault("CITADEL_DATABASE_URL", f"sqlite:///{_TEST_DB.as_posix()}")


@atexit.register
def _remove_test_database() -> None:
    """Keep the temp dir from filling with one .db per run ever performed."""
    for path in (_TEST_DB, Path(f"{_TEST_DB}-wal"), Path(f"{_TEST_DB}-shm")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # a stray temp file is not worth failing a test run over

import pytest  # noqa: E402

from app.db import init_db  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Task, User  # noqa: E402
from app.db.state_machines import Classification, Role  # noqa: E402


@pytest.fixture
def db():
    """A clean schema for every test. The event chain is global state by
    design, so it has to start empty or one test's chain would extend
    another's."""
    init_db.reset()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def engineer(db):
    user = User(
        user_id="U123",
        username="j.rao",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def task(db, engineer):
    row = Task(
        task_id="T123",
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True, "needs_document_generation": True},
    )
    db.add(row)
    db.commit()
    return row
