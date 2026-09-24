"""Tasks as durable rows: submission, the queue, the journal, pause, cancellation.

Execution never happens inside an HTTP request (packages/runtime/AGENTS.md). The API
inserts a task and returns; a worker claims it with `FOR UPDATE SKIP LOCKED`, so any
number of workers can pull at once; every step any agent takes is appended to the task's
journal -- tagged with which agent (or person) took it -- before the next begins. A task
is reconstructible from its journal, which is what makes a worker restart mid-task
resumable, a pause resumable, and a cancellation real.

Two kinds of task: 'task' produces work (and may split it across agents); 'ask' answers
a question typed into the workbench's command line, about documents or about the
workbench's own state.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User
from citadel_memory import WorkingMemory
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database, Json

TERMINAL = frozenset({"completed", "failed", "cancelled"})
ACTIVE = frozenset({"submitted", "planning", "running", "revision_required"})
KINDS = ("task", "ask")
MAX_GOAL = 4000
#: How many times the person who asked for a deliverable may send it back to the agents
#: with an instruction ("add a section on training", "make the summary shorter"). An
#: approver's rejection is separate and still buys exactly one revision (worker.py).
MAX_OWNER_REVISIONS = 5

#: A deliverable superseded by a later version of the same template in the same task is
#: history, not something awaiting a decision.
LATEST_VERSION = (
    "NOT EXISTS (SELECT 1 FROM artifacts n WHERE n.task_id = a.task_id AND n.template_id = a.template_id "
    "AND n.version > a.version)"
)
#: A task whose worker has not written a heartbeat for this long is reclaimable.
STALE_AFTER_S = 180


class TaskError(ValueError):
    pass


_TASK_COLUMNS = (
    "t.id::text AS id, t.goal, t.title, t.kind, t.classification, t.status, t.requirements, t.primary_capability, "
    "t.plan, t.result, t.error, t.cancel_requested, t.pause_requested, t.revision_count, t.worker_id, t.usage, "
    "t.draft_id::text AS draft_id, t.created_at, t.updated_at, t.started_at, t.finished_at, "
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
    kind: str = "task",
    title: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a task. The classification defaults to the submitter's clearance and may
    be set lower, never higher -- and never above the deployment profile's ceiling."""
    if kind not in KINDS:
        raise TaskError(f"unknown kind of task {kind!r}")
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
        "INSERT INTO tasks (goal, title, kind, submitted_by, classification, requirements, draft_id) VALUES "
        "(%(g)s, %(title)s, %(kind)s, (SELECT id FROM users WHERE external_identity = %(u)s), %(c)s, %(r)s, "
        "%(draft)s::uuid) RETURNING id::text AS id",
        {"g": goal, "title": " ".join((title or goal).split())[:90], "kind": kind, "u": user.user_id,
         "c": level.lower(), "r": Json(dict(requirements or {})), "draft": draft_id},
    )
    if row is None:
        raise TaskError("the task could not be created")
    task_id = str(row["id"])
    Journal(db, task_id).append("submitted", {
        "goal": goal, "kind": kind, "classification": level, "by": user.user_id, "name": user.username,
        "department": user.department,
    })
    if audit is not None:
        audit.record("task.submitted", actor_id=user.user_id,
                     payload={"task_id": task_id, "kind": kind, "classification": level, "goal_chars": len(goal)})
    task = get_task(db, task_id)
    assert task is not None
    return task


def get_task(db: Database, task_id: str) -> Optional[dict[str, Any]]:
    return db.query_one(
        f"SELECT {_TASK_COLUMNS} FROM tasks t JOIN users u ON u.id = t.submitted_by WHERE t.id = %(id)s::uuid",
        {"id": task_id},
    )


def list_tasks(db: Database, *, user_id: Optional[str] = None, limit: int = 50,
               kind: Optional[str] = None) -> list[dict[str, Any]]:
    clauses = []
    if user_id:
        clauses.append("u.external_identity = %(u)s")
    if kind:
        clauses.append("t.kind = %(kind)s")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(
        f"SELECT {_TASK_COLUMNS} FROM tasks t JOIN users u ON u.id = t.submitted_by {where} "
        "ORDER BY t.created_at DESC LIMIT %(n)s",
        {"u": user_id, "n": limit, "kind": kind},
    )


def claim_next(db: Database, worker_id: str, *, stale_after_s: int = STALE_AFTER_S) -> Optional[dict[str, Any]]:
    """The oldest runnable task: new, sent back for revision, resumed after a pause, or
    abandoned by a worker that stopped writing its heartbeat. Returns the task with
    `previous_status`. Questions (/ask) go before work: a person is waiting at the
    command line for the answer."""
    claimed = db.query_one(
        "UPDATE tasks t SET status = CASE WHEN p.previous IN ('revision_required', 'running', 'paused') THEN 'running' "
        "ELSE 'planning' END, worker_id = %(w)s, started_at = coalesce(t.started_at, now()) "
        "FROM (SELECT id, status AS previous FROM tasks WHERE NOT cancel_requested AND NOT pause_requested AND ("
        "status IN ('submitted', 'revision_required', 'paused') OR (status IN ('planning', 'running') "
        "AND updated_at < now() - make_interval(secs => %(stale)s))) "
        "ORDER BY (kind = 'ask') DESC, created_at FOR UPDATE SKIP LOCKED LIMIT 1) p "
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
    if task["status"] in ("submitted", "revision_required", "awaiting_approval", "paused"):
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


def interruption(db: Database, task_id: str) -> Optional[str]:
    """'cancel', 'pause' or None -- one query, checked by every agent before every step."""
    row = db.query_one(
        "SELECT cancel_requested, pause_requested FROM tasks WHERE id = %(id)s::uuid", {"id": task_id}
    )
    if row is None:
        return None
    if row["cancel_requested"]:
        return "cancel"
    if row["pause_requested"]:
        return "pause"
    return None


def request_pause(db: Database, task_id: str, *, user: User, audit: Optional[AuditLog] = None) -> dict[str, Any]:
    """Stop every agent at its next step, keeping the journal, the agents and the shared
    state: a paused task resumes where it stopped. A queued task pauses at once."""
    task = get_task(db, task_id)
    if task is None:
        raise TaskError("no such task")
    if task["status"] in TERMINAL or task["status"] in ("awaiting_approval", "paused"):
        return task
    db.execute("UPDATE tasks SET pause_requested = true WHERE id = %(id)s::uuid", {"id": task_id})
    if task["status"] in ("submitted", "revision_required"):
        set_status(db, task_id, "paused")
        Journal(db, task_id).append("paused", {"by": user.user_id, "while": task["status"]})
    else:
        Journal(db, task_id).append("pause_requested", {"by": user.user_id})
    if audit is not None:
        audit.record("task.paused", actor_id=user.user_id, payload={"task_id": task_id, "while": task["status"]})
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


def request_resume(db: Database, task_id: str, *, user: User, audit: Optional[AuditLog] = None) -> dict[str, Any]:
    """Clear the pause; a worker claims the task and every agent carries on from its journal."""
    task = get_task(db, task_id)
    if task is None:
        raise TaskError("no such task")
    if not task["pause_requested"] and task["status"] != "paused":
        return task
    db.execute("UPDATE tasks SET pause_requested = false WHERE id = %(id)s::uuid", {"id": task_id})
    Journal(db, task_id).append("resumed", {"by": user.user_id})
    if audit is not None:
        audit.record("task.resumed", actor_id=user.user_id, payload={"task_id": task_id})
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


def latest_deliverable(db: Database, task_id: str) -> Optional[dict[str, Any]]:
    """The task's current deliverable, with what it was generated from: the newest version
    that passed verification (a later draft that failed it -- a person's edit citing
    something the task never held, say -- is a draft to fix, not the deliverable), or the
    newest version when none has passed yet."""
    return db.query_one(
        "SELECT a.id::text AS id, a.template_id, a.version, a.status, a.requires_approval, a.created_by, "
        "a.provenance -> 'render_inputs' -> 'content' AS content, "
        "a.provenance -> 'render_inputs' ->> 'revision_note' AS note FROM artifacts a "
        "WHERE a.task_id = %(t)s::uuid AND a.template_id IS NOT NULL "
        "ORDER BY (a.status IN ('VERIFIED', 'APPROVED', 'RELEASED')) DESC, a.created_at DESC LIMIT 1",
        {"t": task_id},
    )


def request_revision(db: Database, task_id: str, *, user: User, instruction: str,
                     audit: Optional[AuditLog] = None) -> dict[str, Any]:
    """The person who asked for a deliverable sends it back to the agents with an
    instruction. The agents start from its current version -- including any edits the
    person made by hand -- and produce the next one."""
    task = get_task(db, task_id)
    if task is None:
        raise TaskError("no such task")
    if task["submitted_by"] != user.user_id:
        raise TaskError("only the person who asked for this deliverable may ask for it to be revised")
    if task.get("kind") == "ask":
        raise TaskError("a question is answered, not revised: ask again")
    if task["status"] not in ("completed", "awaiting_approval", "failed"):
        raise TaskError(f"the task is {task['status']}; ask for a revision once the agents have finished")
    instruction = " ".join((instruction or "").split())
    if len(instruction) < 3:
        raise TaskError("say what should change")
    latest = latest_deliverable(db, task_id)
    if latest is None:
        raise TaskError("this task produced no deliverable to revise; submit a new task instead")
    memory = WorkingMemory(db, task_id)
    used = int(memory.get("owner_revisions") or 0)
    if used >= MAX_OWNER_REVISIONS:
        raise TaskError(f"this deliverable has been revised {used} times at your request; edit it by hand or start a new task")
    memory.put("owner_revisions", used + 1)
    memory.put("revision_request", {"comment": instruction[:2000], "by": user.user_id, "name": user.username,
                                    "kind": "owner", "from_artifact": latest["id"], "from_version": latest["version"]})
    db.execute("UPDATE tasks SET cancel_requested = false, pause_requested = false WHERE id = %(id)s::uuid", {"id": task_id})
    set_status(db, task_id, "revision_required", error=None)
    Journal(db, task_id, agent_id=f"human:{user.user_id}").append("revision_requested", {
        "comment": instruction[:1000], "by": user.user_id, "name": user.username, "kind": "owner",
        "from_version": latest["version"],
    })
    if audit is not None:
        audit.record("task.revision_requested", actor_id=user.user_id,
                     payload={"task_id": task_id, "kind": "owner", "from_artifact": latest["id"]})
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


class Journal:
    """Append-only, per task. Sequence numbers are dense and ordered across every agent
    (and person) writing to the task: each append takes the task's journal lock for the
    one statement that numbers and inserts it, so concurrent agents never collide."""

    def __init__(self, db: Database, task_id: str, *, agent_id: Optional[str] = None) -> None:
        self.db = db
        self.task_id = task_id
        self.agent_id = agent_id

    def append(self, step_type: str, payload: Mapping[str, Any]) -> int:
        rows = self.db.serialized(
            f"citadel:journal:{self.task_id}",
            [],
            (
                "INSERT INTO task_journal (task_id, step_seq, step_type, payload, agent_id) "
                "SELECT %(t)s::uuid, coalesce(max(step_seq), 0) + 1, %(k)s, %(p)s, %(a)s FROM task_journal "
                "WHERE task_id = %(t)s::uuid RETURNING step_seq",
                {"t": self.task_id, "k": step_type, "p": Json(dict(payload)), "a": self.agent_id},
            ),
        )
        return int(rows[0]["step_seq"]) if rows else 0

    def entries(self, after: int = 0, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT step_seq, step_type, payload, agent_id, created_at FROM task_journal "
            "WHERE task_id = %(t)s::uuid AND step_seq > %(a)s ORDER BY step_seq LIMIT %(n)s",
            {"t": self.task_id, "a": after, "n": limit},
        )

    def entries_of(self, agent_ids: Sequence[Optional[str]], *, after: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        """One agent's own entries (None stands for entries written before agents had ids)."""
        named = [a for a in agent_ids if a is not None]
        include_null = any(a is None for a in agent_ids)
        return self.db.query(
            "SELECT step_seq, step_type, payload, agent_id, created_at FROM task_journal "
            "WHERE task_id = %(t)s::uuid AND step_seq > %(a)s AND (agent_id = ANY(%(ids)s)"
            + (" OR agent_id IS NULL" if include_null else "") + ") ORDER BY step_seq LIMIT %(n)s",
            {"t": self.task_id, "a": after, "ids": named or ["-"], "n": limit},
        )

    def human_steps(self, *, after: int = 0) -> list[dict[str, Any]]:
        """What people did in the workbench on this task since `after`."""
        return self.db.query(
            "SELECT step_seq, step_type, payload, agent_id, created_at FROM task_journal "
            "WHERE task_id = %(t)s::uuid AND step_seq > %(a)s AND agent_id LIKE 'human:%%' ORDER BY step_seq",
            {"t": self.task_id, "a": after},
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
    "interruption",
    "request_pause",
    "request_resume",
    "request_revision",
    "latest_deliverable",
    "KINDS",
    "MAX_OWNER_REVISIONS",
    "LATEST_VERSION",
    "Journal",
]
