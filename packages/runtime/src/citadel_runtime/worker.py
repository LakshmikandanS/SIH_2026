"""The worker pool and the task-side consequences of an approval decision.

Task concurrency is unbounded in principle and bounded here only by how many worker
threads a process runs; GPU admission is a separate, smaller bound inside the gateway,
so workers pull tasks freely and queue only at the model call (packages/runtime
AGENTS.md, "Two caps, not one"). A heartbeat thread keeps every running task's
`updated_at` fresh, so a task is reclaimed only when its worker has actually died.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Optional

from citadel_contracts.domain import User
from citadel_memory import MemoryManager, WorkingMemory, remember_decision
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database

from citadel_runtime.agent import Runtime, run_task
from citadel_runtime.tasks import LATEST_VERSION, Journal, claim_next, get_task, heartbeat, set_status

#: One bounded revision cycle after a rejection (packages/deliverables/AGENTS.md).
MAX_REVISIONS = 1


class Worker:
    def __init__(self, rt: Runtime, worker_id: str, *, concurrency: int = 3, poll_s: float = 1.0) -> None:
        self.rt = rt
        self.worker_id = worker_id
        self.concurrency = max(1, concurrency)
        self.poll_s = poll_s
        self._running: dict[str, float] = {}
        self._lock = threading.Lock()

    def run_once(self) -> Optional[str]:
        """Claim and run one task to its end, synchronously. Returns its final status."""
        task = claim_next(self.rt.db, self.worker_id)
        if task is None:
            return None
        return self._run(task)

    def _run(self, task: dict[str, Any]) -> str:
        task_id = str(task["id"])
        with self._lock:
            self._running[task_id] = time.time()
        try:
            return run_task(self.rt, task, self.worker_id)
        except Exception as exc:  # construction failures land here; the task still ends
            traceback.print_exc(file=sys.stderr)
            set_status(self.rt.db, task_id, "failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
            Journal(self.rt.db, task_id).append("failed", {"error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            return "failed"
        finally:
            with self._lock:
                self._running.pop(task_id, None)

    def running(self) -> list[str]:
        with self._lock:
            return list(self._running)

    def serve(self, stop: threading.Event) -> None:
        threads = [
            threading.Thread(target=self._loop, args=(stop,), name=f"{self.worker_id}-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        beat = threading.Thread(target=self._heartbeat, args=(stop,), name=f"{self.worker_id}-heartbeat", daemon=True)
        for thread in [*threads, beat]:
            thread.start()
        while not stop.is_set():
            stop.wait(1.0)
        for thread in threads:
            thread.join(timeout=5)

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                task = claim_next(self.rt.db, self.worker_id)
            except Exception as exc:
                print(f"[worker] claim failed: {exc}", file=sys.stderr)
                stop.wait(5.0)
                continue
            if task is None:
                stop.wait(self.poll_s)
                continue
            self._run(task)

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(30.0):
            for task_id in self.running():
                try:
                    heartbeat(self.rt.db, task_id)
                except Exception as exc:
                    print(f"[worker] heartbeat for {task_id} failed: {exc}", file=sys.stderr)


def after_decision(
    db: Database,
    *,
    task_id: str,
    approved: bool,
    comment: str,
    approver: User,
    audit: Optional[AuditLog] = None,
    gateway: Any = None,
    artifact_title: Optional[str] = None,
    remember: bool = True,
) -> dict[str, Any]:
    """Move the task on after an approver's decision on one of its artifacts: approval
    completes it once nothing else awaits a decision; rejection sends it back for its one
    bounded revision, or ends it if that revision has already been spent.

    The decision is also remembered (episodic, in the task's compartment): a rejection
    comment is exactly what the next task of the same kind should meet before it starts."""
    task = get_task(db, task_id)
    if task is None:
        raise ValueError("no such task")
    journal = Journal(db, task_id)
    journal.append("decision", {"approved": approved, "comment": comment[:1000], "by": approver.user_id, "name": approver.username})
    if remember:
        _remember_decision(db, task, approved=approved, comment=comment, approver=approver, audit=audit,
                           gateway=gateway, artifact_title=artifact_title, journal=journal)
    if approved:
        waiting = db.scalar(
            "SELECT count(*) FROM artifacts a WHERE a.task_id = %(t)s::uuid AND a.requires_approval "
            f"AND a.status = 'VERIFIED' AND {LATEST_VERSION} "
            "AND NOT EXISTS (SELECT 1 FROM approvals p WHERE p.artifact_id = a.id)",
            {"t": task_id},
        )
        if not waiting:
            set_status(db, task_id, "completed")
            journal.append("finished", {"status": "completed", "released": True})
    else:
        revisions = int(task.get("revision_count") or 0)
        if revisions < MAX_REVISIONS:
            WorkingMemory(db, task_id).put("revision_request", {"comment": comment, "by": approver.user_id})
            set_status(db, task_id, "revision_required", revision_count=revisions + 1)
            journal.append("revision_requested", {"comment": comment[:1000], "revision": revisions + 1})
        else:
            set_status(db, task_id, "failed", error="the deliverable was rejected after its one revision")
            journal.append("failed", {"error": "the deliverable was rejected after its one revision"})
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


def after_edit(
    db: Database,
    *,
    task_id: str,
    editor: User,
    artifact_id: str,
    version: int,
    status: str,
    requires_approval: bool,
    note: str,
    audit: Optional[AuditLog] = None,
) -> dict[str, Any]:
    """A person edited the deliverable by hand and it was re-rendered and re-verified as
    a new version. Journal it like any step; if the new version verified, it is now the
    deliverable -- awaiting a decision if its template asks for one, final otherwise. A
    version that failed verification changes nothing but the record: the last verified
    version still stands, and the person sees which tier to fix."""
    task = get_task(db, task_id)
    if task is None:
        raise ValueError("no such task")
    Journal(db, task_id, agent_id=f"human:{editor.user_id}").append("human", {
        "by": editor.user_id, "name": editor.username, "action": "edited", "kind": "deliverable",
        "summary": note or "edited the deliverable", "artifact_id": artifact_id, "version": version, "status": status,
    })
    if audit is not None:
        audit.record("artifact.edited", actor_id=editor.user_id,
                     payload={"task_id": task_id, "artifact_id": artifact_id, "version": version, "status": status})
    if status != "VERIFIED":
        return task
    result = dict(task.get("result") or {})
    result["artifacts"] = db.query(
        "SELECT id::text AS id, title, template_id, filename, status, requires_approval, version, kind FROM artifacts "
        "WHERE task_id = %(t)s::uuid ORDER BY created_at", {"t": task_id},
    )
    result["awaiting_approval"] = [artifact_id] if requires_approval else []
    set_status(db, task_id, "awaiting_approval" if requires_approval else "completed", result=result)
    refreshed = get_task(db, task_id)
    assert refreshed is not None
    return refreshed


def _remember_decision(
    db: Database,
    task: dict[str, Any],
    *,
    approved: bool,
    comment: str,
    approver: User,
    audit: Optional[AuditLog],
    gateway: Any,
    artifact_title: Optional[str],
    journal: Journal,
) -> None:
    """Best effort: a memory that could not be written never changes the decision."""
    try:
        outcome = remember_decision(
            MemoryManager(db, gateway, audit), task=task, approved=approved, comment=comment,
            approver_name=approver.username, artifact_title=artifact_title or str(task.get("title") or "the deliverable"),
            classification=str(task["classification"]), department=str(task.get("department") or approver.department),
            actor_id=approver.user_id,
        )
        journal.append("memory", {"outcomes": [outcome.to_dict()], "summary": f"1 {outcome.operation}"})
    except Exception as exc:
        journal.append("memory", {"error": f"{type(exc).__name__}: {str(exc)[:300]}", "outcomes": []})


__all__ = ["Worker", "after_decision", "after_edit", "MAX_REVISIONS"]
