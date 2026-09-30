"""Authoritative Task-state commits (design doc section 6.11).

    "Only the Orchestrator commits authoritative task state [...] Optimistic
     versioning is used for the one place true concurrent writes could occur
     [...]: `expected_version` must match, else `409 CONFLICT` [...] a
     conflict here means retry the read-modify-write once, never a semantic
     merge of concurrent agent outputs."

Every Task status change this package makes goes through
`commit_task_transition` below, so the `expected_version` check
(`app/db/transitions.py`, built by `foundation-schema`) and the
`STATE_COMMITTED` event are never skipped at one call site and forgotten at
another. Because this MVP runs one task at a time with a single sequential
agent loop (design doc section 6.11's own words), a real conflict never
actually arises against the live database -- the retry-once behaviour is
still implemented, not merely asserted, and is exercised directly by
`tests/test_orchestration.py` using the `expected_version` override below.
"""

from __future__ import annotations

from typing import Optional

from app.db.engine import SessionLocal
from app.db.models import Task
from app.db.transitions import VersionConflict, transition_task
from app.observability import EventType, append_event


def commit_task_transition(
    task_id: str, new_status: str, *, expected_version: Optional[int] = None
) -> Task:
    """Move `Task.status` to `new_status`, emit `STATE_COMMITTED`, retry once
    on a version conflict.

    `expected_version`, when given, is used only on the *first* attempt --
    this is what lets a caller (a test) force a genuine `VersionConflict` to
    prove the retry path, without ever letting production code accidentally
    pin a stale version across the retry (the second attempt always reads
    the row fresh).
    """
    stale_hint = expected_version
    last_error: Optional[VersionConflict] = None

    for attempt in range(2):
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"unknown task {task_id!r}")

            version_to_check = stale_hint if (attempt == 0 and stale_hint is not None) else task.version
            try:
                transition_task(session, task, new_status, expected_version=version_to_check)
            except VersionConflict as exc:
                session.rollback()
                last_error = exc
                stale_hint = None  # drop the stale hint; the retry reads fresh
                continue

            append_event(
                task_id,
                None,
                EventType.STATE_COMMITTED,
                {"entity": "task", "new_status": new_status, "version": task.version},
                session=session,
            )
            session.commit()
            session.refresh(task)
            session.expunge(task)
            return task

    assert last_error is not None  # pragma: no cover -- defensive only
    raise last_error
