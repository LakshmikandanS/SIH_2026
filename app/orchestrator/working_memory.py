"""Working Memory (design doc section 6.9's own naming) for one agent run.

    "Working Memory is not persisted at all; it lives only in the
     Orchestrator's in-process call stack for the duration of one synchronous
     task execution."

`WorkingMemory` itself is that call-stack object: the THINK step of each
later plan step reads it to decide concrete arguments (section 5.1 -- "given
the step's action + current evidence, decide the concrete arguments"), and
nothing here is a database row.

The one deliberate, narrow exception is the module-level `_STORE` below,
which exists *solely* to make section 5.3's plan revision possible without
re-running `rag.search`/`python.execute` (section 5.3 rule 1: "Keep all
completed steps' evidence and events untouched"). A human's approval
decision is a genuinely separate request from the one that ran the agent
loop -- by the time it arrives, the loop's own call stack is long gone. This
in-process dict is NOT a persistence layer: it is keyed by `task_id`, never
written to disk, and lost on a process restart exactly like every other
piece of unpersisted state in this MVP (section 7, BB-012 -- "a crashed
process leaves the task in its last recorded status"). It is the smallest
thing that lets one revision reuse the original evidence rather than either
re-querying the Data Plane (which section 5.3 forbids -- only
`generate_report` re-runs) or persisting Working Memory in the database
(which section 6.9 forbids outright). See docs/BUILD_LOG.md's Step 7 section
for this being flagged as a genuine tension in the frozen design and the
resolution chosen here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WorkingMemory:
    """Everything one task's agent loop has observed so far."""

    task_id: str
    #: S1's `rag.search` result rows (design doc section 3's Evidence shape).
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: S2's `python.execute` result, parsed from stdout as JSON (the shape
    #: `app.orchestrator.think`'s generated code prints), or `None` if stdout
    #: was not JSON / was empty.
    computed: Optional[dict[str, Any]] = None
    #: S2's raw tool result (`stdout`/`stderr`/`exit_code`), kept for the
    #: trace and for a THINK step that might want the unparsed text.
    raw_python_result: Optional[dict[str, Any]] = None
    #: Set once S3 (`generate_report`) succeeds.
    artifact_id: Optional[str] = None
    artifact_path: Optional[str] = None

    def combined_evidence_text(self) -> str:
        """All retrieved evidence, concatenated in retrieval order -- what
        `python.execute`'s generated code (S2) actually parses dates out of.
        """
        return "\n\n".join(row.get("text", "") for row in self.evidence)

    def provenance_ids(self) -> list[str]:
        return [row["evidence_id"] for row in self.evidence if "evidence_id" in row]


_lock = threading.Lock()
_STORE: dict[str, WorkingMemory] = {}


def get_or_create(task_id: str) -> WorkingMemory:
    with _lock:
        memory = _STORE.get(task_id)
        if memory is None:
            memory = WorkingMemory(task_id=task_id)
            _STORE[task_id] = memory
        return memory


def save(memory: WorkingMemory) -> None:
    with _lock:
        _STORE[memory.task_id] = memory


def load(task_id: str) -> Optional[WorkingMemory]:
    with _lock:
        return _STORE.get(task_id)


def clear(task_id: str) -> None:
    with _lock:
        _STORE.pop(task_id, None)


def reset() -> None:
    """Test-suite reset only -- mirrors `app.tool_gateway.clear_backends`."""
    with _lock:
        _STORE.clear()
