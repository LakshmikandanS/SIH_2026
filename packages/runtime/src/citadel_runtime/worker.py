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
from citadel_memory import WorkingMemory
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database

from citadel_runtime.agent import Runtime, run_task
from citadel_runtime.tasks import Journal, claim_next, get_task, heartbeat, set_status

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
) -> dict[str, Any]:
    """Move the task on after an approver's decision on one of its artifacts: approval
    completes it once nothing else awaits a decision; rejection sends it back for its one
    bounded revision, or ends it if that revision has already been spent."""
    task = get_task(db, task_id)
    if task is None:
        raise ValueError("no such task")
    journal = Journal(db, task_id)
    journal.append("decision", {"approved": approved, "comment": comment[:1000], "by": approver.user_id, "name": approver.username})
    if approved:
        waiting = db.scalar(
            "SELECT count(*) FROM artifacts a WHERE a.task_id = %(t)s::uuid AND a.requires_approval "
            "AND a.status = 'VERIFIED' AND NOT EXISTS (SELECT 1 FROM approvals p WHERE p.artifact_id = a.id)",
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


__all__ = ["Worker", "after_decision", "MAX_REVISIONS"]
