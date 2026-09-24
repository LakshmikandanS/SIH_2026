"""Three tiers of memory, all Citadel-side, all scoped.

* **Working** -- one task's structured scratchpad (the plan, the revision request),
  `task_memory` (migration 0008), written deliberately by the runtime.
* **Episodic** -- across tasks: outcomes, decisions, rejections.
* **Semantic** -- durable organisational facts: equipment, costs, vendors, conventions.

The long-term tiers are the memory manager (`manager.py`): Monarch's design -- extract
candidate memories, decide one of six mutations against the related ones, execute it
deterministically, retrieve by meaning and recency -- in Citadel's Postgres, with the
visibility predicate (classification and ACL, like documents) inside the same SQL
statement as the vector search, which is the seam Monarch's own store still lacks
(docs/adr/0009). `EpisodicMemory`/`SemanticMemory` stay the Protocols a future
Monarch-backed store would implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from citadel_platform.db import Database, Json

from citadel_memory.consolidate import remember_decision, remember_task
from citadel_memory.manager import (
    OPERATIONS,
    SUGGESTED_TYPES,
    TIERS,
    Candidate,
    Compartment,
    Memory,
    MemoryManager,
    Outcome,
    allowed_levels,
)
from citadel_memory.scope import MemoryScope


class MemoryNotConfigured(RuntimeError):
    pass


class WorkingMemory:
    """One task's memory. Values are JSON; keys are the agent's own vocabulary."""

    def __init__(self, db: Database, task_id: str) -> None:
        self.db = db
        self.task_id = task_id

    def put(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO task_memory (task_id, key, value) VALUES (%(t)s::uuid, %(k)s, %(v)s) "
            "ON CONFLICT (task_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            {"t": self.task_id, "k": key[:200], "v": Json(value)},
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one(
            "SELECT value FROM task_memory WHERE task_id = %(t)s::uuid AND key = %(k)s",
            {"t": self.task_id, "k": key},
        )
        return default if row is None else row["value"]

    def all(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT key, value FROM task_memory WHERE task_id = %(t)s::uuid ORDER BY key", {"t": self.task_id}
        )
        return {str(r["key"]): r["value"] for r in rows}

    def append(self, key: str, item: Any, *, limit: int = 50) -> list[Any]:
        items = list(self.get(key, []) or [])
        items.append(item)
        items = items[-limit:]
        self.put(key, items)
        return items


@dataclass(frozen=True)
class MemoryRecord:
    kind: str  # open vocabulary: decision, outcome, rejection, equipment_fact, convention...
    text: str
    scope_key: str
    attributes: Mapping[str, Any]


class EpisodicMemory(Protocol):
    """Across tasks: decisions, outcomes, rejections."""

    def remember(self, record: MemoryRecord) -> str: ...

    def recall(self, query: str, scope: MemoryScope, *, top_k: int = 5) -> Sequence[MemoryRecord]: ...


class SemanticMemory(Protocol):
    """Durable organisational facts: equipment, vendors, conventions, people, house style."""

    def assert_fact(self, record: MemoryRecord) -> str: ...

    def facts(self, subject: str, scope: MemoryScope) -> Sequence[MemoryRecord]: ...


def status() -> dict[str, Optional[str]]:
    return {
        "working": "postgres (task_memory)",
        "episodic": "postgres + pgvector (memories, tier episodic)",
        "semantic": "postgres + pgvector (memories, tier semantic)",
        "note": "the memory manager: Monarch's extract/mutate/retrieve design in Citadel's scoped store",
    }


__all__ = [
    "WorkingMemory",
    "MemoryScope",
    "MemoryRecord",
    "EpisodicMemory",
    "SemanticMemory",
    "MemoryNotConfigured",
    "MemoryManager",
    "Memory",
    "Candidate",
    "Compartment",
    "Outcome",
    "OPERATIONS",
    "TIERS",
    "SUGGESTED_TYPES",
    "allowed_levels",
    "remember_task",
    "remember_decision",
    "status",
]
