"""The single serializing Observability writer (design doc section 6.12).

This module is the *only* code in the system that inserts into the `events`
table. Every component calls `append_event`; nothing else computes a hash or
appends to the chain.

Why a lock and not a queue: BB-035's black box was ordering, not cryptography
-- with five-plus concurrent emitters there was no defined predecessor for a
given event. Section 6.12 sidesteps that rather than solving it: one writer,
single-threaded by construction, so there is exactly one place total ordering
could break and it is serialized.

The Phase-2 seam: `EventWriter` is a class with a small interface
(`append_event` / `get_trace` / `verify_chain`) and `_default_writer` is one
instance of it. Moving the chain into its own process, or fronting it with a
durable queue, means providing another object with the same three methods and
pointing `set_writer` at it -- callers import the module-level functions and
never see which implementation answered.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, ids
from app.db.base import utcnow
from app.db.engine import SessionLocal
from app.db.models import Event
from app.observability import hashing
from app.observability.event_types import EVENT_TYPE_SET, UnknownEventType


class ChainBroken(Exception):
    """Raised by `verify_chain` when the recorded chain does not recompute."""


class WriterProtocol(Protocol):
    def append_event(
        self,
        task_id: Optional[str],
        actor_id: Optional[str],
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        session: Optional[Session] = None,
    ) -> Event: ...

    def get_trace(self, task_id: Optional[str] = None) -> list[Event]: ...

    def verify_chain(self, task_id: Optional[str] = None) -> bool: ...


class EventWriter:
    """Serializing, hash-chaining, append-only event writer."""

    def __init__(self, strategy_name: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._strategy_name = strategy_name or config.EVENT_HASH_STRATEGY
        self._strategy = hashing.get_strategy(self._strategy_name)

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    # -- write path ------------------------------------------------------
    def append_event(
        self,
        task_id: Optional[str],
        actor_id: Optional[str],
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        session: Optional[Session] = None,
    ) -> Event:
        """Append one event to the chain and return it.

        `session` lets a caller enlist the event in a transaction it already
        owns -- section 6.10's approval decision has to move Approval,
        Artifact, Task *and* its two events atomically, so the events cannot
        be written on a separate connection that commits independently.
        When omitted, the writer opens and commits its own session.
        """
        if event_type not in EVENT_TYPE_SET:
            raise UnknownEventType(event_type)

        payload = dict(payload or {})

        with self._lock:
            if session is not None:
                return self._append_in_session(
                    session, task_id, actor_id, event_type, payload
                )
            own = SessionLocal()
            try:
                event = self._append_in_session(
                    own, task_id, actor_id, event_type, payload
                )
                own.commit()
                own.refresh(event)
                return event
            except Exception:
                own.rollback()
                raise
            finally:
                own.close()

    def _append_in_session(
        self,
        session: Session,
        task_id: Optional[str],
        actor_id: Optional[str],
        event_type: str,
        payload: dict[str, Any],
    ) -> Event:
        # Predecessor is read inside the lock, so no two events can claim the
        # same one.
        last = session.execute(
            select(Event).order_by(Event.seq.desc()).limit(1)
        ).scalar_one_or_none()

        previous_hash = last.hash if last is not None else config.GENESIS_HASH
        seq = (last.seq + 1) if last is not None else 1

        timestamp = utcnow()
        record = {
            "event_id": ids.format_event_id(seq),
            "task_id": task_id,
            "actor_id": actor_id,
            "event_type": event_type,
            "payload": payload,
            "timestamp": timestamp.isoformat(),
        }
        event_hash = self._strategy(record, previous_hash)

        event = Event(
            seq=seq,
            event_id=record["event_id"],
            task_id=task_id,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
            timestamp=timestamp,
            previous_hash=previous_hash,
            hash=event_hash,
        )
        session.add(event)
        session.flush()
        return event

    # -- read path -------------------------------------------------------
    def get_trace(self, task_id: Optional[str] = None) -> list[Event]:
        """Events in chain order. `task_id=None` returns the whole chain,
        which is what the chain-integrity check and the admin view need."""
        with SessionLocal() as session:
            stmt = select(Event).order_by(Event.seq.asc())
            if task_id is not None:
                stmt = stmt.where(Event.task_id == task_id)
            return list(session.execute(stmt).scalars().all())

    def verify_chain(self, task_id: Optional[str] = None) -> bool:
        """Recompute the whole chain and compare.

        Always verifies the *global* chain even when reporting on one task: a
        per-task subsequence has gaps by construction (other tasks' events sit
        between its links), so verifying a filtered slice would prove nothing.
        `task_id` only scopes the error message.
        """
        events = self.get_trace(None)
        previous_hash = config.GENESIS_HASH

        for index, event in enumerate(events):
            if event.seq != index + 1:
                raise ChainBroken(
                    f"sequence gap at {event.event_id}: expected seq "
                    f"{index + 1}, found {event.seq}"
                )
            if event.previous_hash != previous_hash:
                raise ChainBroken(
                    f"broken link at {event.event_id}: previous_hash "
                    f"{event.previous_hash[:12]}... does not match predecessor "
                    f"hash {previous_hash[:12]}..."
                )
            record = {
                "event_id": event.event_id,
                "task_id": event.task_id,
                "actor_id": event.actor_id,
                "event_type": event.event_type,
                "payload": event.payload,
                "timestamp": _iso(event.timestamp),
            }
            recomputed = self._strategy(record, previous_hash)
            if recomputed != event.hash:
                raise ChainBroken(
                    f"tampered content at {event.event_id} "
                    f"({event.event_type}): stored hash {event.hash[:12]}... "
                    f"does not match recomputed {recomputed[:12]}..."
                )
            previous_hash = event.hash

        if task_id is not None and not any(e.task_id == task_id for e in events):
            raise ChainBroken(f"no events recorded for task {task_id}")
        return True


def _iso(value: datetime) -> str:
    """Re-render a timestamp exactly as the write path did.

    SQLite drops tzinfo on read, so a naive value coming back from the DB is
    UTC by construction (`utcnow` is the only producer) and is re-marked as
    such before hashing.
    """
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# --- module-level singleton and the functions every component calls ---------
_default_writer: WriterProtocol = EventWriter()


def set_writer(writer: WriterProtocol) -> WriterProtocol:
    """Swap the writer implementation (a separate event service, a queue-backed
    writer, a test double). Returns the previous one so it can be restored."""
    global _default_writer
    previous = _default_writer
    _default_writer = writer
    return previous


def get_writer() -> WriterProtocol:
    return _default_writer


def append_event(
    task_id: Optional[str],
    actor_id: Optional[str],
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    session: Optional[Session] = None,
) -> Event:
    """THE write path for the event table. Nothing else inserts into it."""
    return _default_writer.append_event(
        task_id, actor_id, event_type, payload, session=session
    )


def get_trace(task_id: Optional[str] = None) -> list[Event]:
    return _default_writer.get_trace(task_id)


def verify_chain(task_id: Optional[str] = None) -> bool:
    return _default_writer.verify_chain(task_id)


def format_trace(events: Sequence[Event]) -> str:
    """Human-readable rendering used by the CLI's /trace (step 9) and by the
    QA definition-of-done script."""
    lines = []
    for event in events:
        lines.append(
            f"{event.event_id}  {_iso(event.timestamp)}  "
            f"{event.event_type:<20}  task={event.task_id or '-':<10} "
            f"actor={event.actor_id or '-':<10} hash={event.hash[:12]}..."
        )
    return "\n".join(lines)
