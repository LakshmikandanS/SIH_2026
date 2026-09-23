"""Tasks as durable rows: submission, the queue, the journal, cancellation.

Execution never happens inside an HTTP request (packages/runtime/AGENTS.md). The API
inserts a task and returns; a worker claims it with `FOR UPDATE SKIP LOCKED`, so any
number of workers can pull at once; every step the agent takes is appended to the task's
journal before the next begins. A task is reconstructible from its journal, which is
what makes a worker restart mid-task resumable and a cancellation real.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database, Json

TERMINAL = frozenset({"completed", "failed", "cancelled"})
ACTIVE = frozenset({"submitted", "planning", "running", "revision_required"})
MAX_GOAL = 4000
#: A task whose worker has not written a heartbeat for this long is reclaimable.
STALE_AFTER_S = 180


class TaskError(ValueError):
    pass


_TASK_COLUMNS = (
    "t.id::text AS id, t.goal, t.title, t.classification, t.status, t.requirements, t.primary_capability, "
    "t.plan, t.result, t.error, t.cancel_requested, t.revision_count, t.worker_id, t.usage, "
    "t.created_at, t.updated_at, t.started_at, t.finished_at, "
    "u.external_identity AS submitted_by, u.display_name AS submitted_by_name, u.department"
)


def submit(
    db: Database,
    *,
    user: User,
    goal: str,
    classification: Optional[str],
    profile_ceiling: str,
    requirements: Optional[Mapping[str, Any]] = None,
    audit: Optional[AuditLog] = None,
) -> dict[str, Any]:
    """Create a task. The classification defaults to the submitter's clearance and may
    be set lower, never higher -- and never above the deployment profile's ceiling."""
    goal = (goal or "").strip()
    if not goal:
        raise TaskError("a task needs a goal")
    if len(goal) > MAX_GOAL:
        raise TaskError(f"the goal is {len(goal)} characters; the limit is {MAX_GOAL}")
    level = (classification or user.clearance).strip().upper()
    try:
        Classification.rank(level)
    except ValueError:
        raise TaskError(f"unknown classification {classification!r}") from None
    if Classification.exceeds(level, user.clearance.upper()):
        raise TaskError(f"{level} is above your clearance ({user.clearance.upper()})")
    if Classification.exceeds(level, profile_ceiling.upper()):
        raise TaskError(f"{level} is above this deployment's ceiling ({profile_ceiling.upper()})")
    row = db.query_one(
        "INSERT INTO tasks (goal, title, submitted_by, classification, requirements) VALUES "
        "(%(g)s, %(title)s, (SELECT id FROM users WHERE external_identity = %(u)s), %(c)s, %(r)s) "
        "RETURNING id::text AS id",
        {"g": goal, "title": " ".join(goal.split())[:90], "u": user.user_id, "c": level.lower(),
         "r": Json(dict(requirements or {}))},
    )
    if row is None:
        raise TaskError("the task could not be created")
    task_id = str(row["id"])
    Journal(db, task_id).append("submitted", {
        "goal": goal, "classification": level, "by": user.user_id, "name": user.username, "department": user.department,
    })
    if audit is not None:
        audit.record("task.submitted", actor_id=user.user_id,
                     payload={"task_id": task_id, "classification": level, "goal_chars": len(goal)})
    task = get_task(db, task_id)
    assert task is not None
    return task


def get_task(db: Database, task_id: str) -> Optional[dict[str, Any]]:
    return db.query_one(
        f"SELECT {_TASK_COLUMNS} FROM tasks t JOIN users u ON u.id = t.submitted_by WHERE t.id = %(id)s::uuid",
        {"id": task_id},
    )


def list_tasks(db: Database, *, user_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    where = "WHERE u.external_identity = %(u)s" if user_id else ""
    return db.query(
        f"SELECT {_TASK_COLUMNS} FROM tasks t JOIN users u ON u.id = t.submitted_by {where} "
        "ORDER BY t.created_at DESC LIMIT %(n)s",
        {"u": user_id, "n": limit},
    )


def claim_next(db: Database, worker_id: str, *, stale_after_s: int = STALE_AFTER_S) -> Optional[dict[str, Any]]:
    """The oldest runnable task: new, sent back for revision, or abandoned by a worker
    that stopped writing its heartbeat. Returns the task with `previous_status`."""
    claimed = db.query_one(
        "UPDATE tasks t SET status = CASE WHEN p.previous IN ('revision_required', 'running') THEN 'running' "
        "ELSE 'planning' END, worker_id = %(w)s, started_at = coalesce(t.started_at, now()) "
        "FROM (SELECT id, status AS previous FROM tasks WHERE NOT cancel_requested AND ("
        "status IN ('submitted', 'revision_required') OR (status IN ('planning', 'running') "
        "AND updated_at < now() - make_interval(secs => %(stale)s))) "
        "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) p "
        "WHERE t.id = p.id RETURNING t.id::text AS id, p.previous AS previous_status",
        {"w": worker_id, "stale": stale_after_s},
    )
    if claimed is None:
        return None
    task = get_task(db, str(claimed["id"]))
    if task is None:
        return None
    task["previous_status"] = claimed["previous_status"]
    return task


def heartbeat(db: Database, task_id: str) -> None:
    db.execute("UPDATE tasks SET updated_at = now() WHERE id = %(id)s::uuid", {"id": task_id})


def set_status(db: Database, task_id: str, status: str, **fields: Any) -> None:
    assignments = ["status = %(status)s"]
    params: dict[str, Any] = {"id": task_id, "status": status}
    for key in ("plan", "result", "usage", "primary_capability", "error", "revision_count"):
        if key in fields:
            assignments.append(f"{key} = %({key})s")
            value = fields[key]
            params[key] = Json(value) if key in ("plan", "result", "usage") else value
    if status in TERMINAL:
        assignments.append("finished_at = now()")
    db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = %(id)s::uuid", params)


def request_cancel(db: Database, task_id: str, *, user: User, audit: Optional[AuditLog] = None) -> dict[str, Any]:
    task = get_task(db, task_id)
    if task is None:
        raise TaskError("no such task")
    if task["status"] in TERMINAL:
        return task
    db.execute("UPDATE tasks SET cancel_requested = true WHERE id = %(id)s::uuid", {"id": task_id})
    if task["status"] in ("submitted", "revision_required", "awaiting_approval"):
        # Nothing is running it: cancel now rather than waiting for a worker to notice.
        set_status(db, task_id, "cancelled", error=f"cancelled by {user.user_id}")
        Journal(db, task_id).append("cancelled", {"by": user.user_id})
        if audit is not None:
            audit.record("task.cancelled", actor_id=user.user_id, payload={"task_id": task_id, "while": task["status"]})
    else:
        Journal(db, task_id).append("cancel_requested", {"by": user.user_id})
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


def is_cancel_requested(db: Database, task_id: str) -> bool:
    return bool(db.scalar("SELECT cancel_requested FROM tasks WHERE id = %(id)s::uuid", {"id": task_id}))


class Journal:
    """Append-only, per task. Sequence numbers are dense and ordered; two writers racing
    for the same number retry rather than overwrite."""

    def __init__(self, db: Database, task_id: str) -> None:
        self.db = db
        self.task_id = task_id

    def append(self, step_type: str, payload: Mapping[str, Any]) -> int:
        for attempt in range(5):
            try:
                seq = self.db.scalar(
                    "INSERT INTO task_journal (task_id, step_seq, step_type, payload) "
                    "SELECT %(t)s::uuid, coalesce(max(step_seq), 0) + 1, %(k)s, %(p)s FROM task_journal "
                    "WHERE task_id = %(t)s::uuid RETURNING step_seq",
                    {"t": self.task_id, "k": step_type, "p": Json(dict(payload))},
                )
                return int(seq or 0)
            except Exception as exc:
                if "duplicate key" not in str(exc) or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        return 0

    def entries(self, after: int = 0, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT step_seq, step_type, payload, created_at FROM task_journal "
            "WHERE task_id = %(t)s::uuid AND step_seq > %(a)s ORDER BY step_seq LIMIT %(n)s",
            {"t": self.task_id, "a": after, "n": limit},
        )


__all__ = [
    "TaskError",
    "TERMINAL",
    "ACTIVE",
    "submit",
    "get_task",
    "list_tasks",
    "claim_next",
    "heartbeat",
    "set_status",
    "request_cancel",
    "is_cancel_requested",
    "Journal",
]
