"""Schema creation.

`create_all` is enough for this slice -- there is no migration history to
preserve and no production data. When Phase 2 needs migrations, Alembic points
at the same `Base.metadata` and this module becomes its bootstrap.
"""

from __future__ import annotations

from app.db import models  # noqa: F401  (registers all six tables on Base)
from app.db.base import Base
from app.db.engine import engine


def create_all() -> None:
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Used by the test suite's fixtures. Never called by the application."""
    Base.metadata.drop_all(bind=engine)


def reset() -> None:
    drop_all()
    create_all()


def table_names() -> list[str]:
    return sorted(Base.metadata.tables)


if __name__ == "__main__":  # pragma: no cover
    create_all()
    print("Created tables:", ", ".join(table_names()))
