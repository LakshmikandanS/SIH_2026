"""SQLAlchemy declarative base and shared column helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Timezone-aware UTC now. Every timestamp in the system uses this."""
    return datetime.now(timezone.utc)
