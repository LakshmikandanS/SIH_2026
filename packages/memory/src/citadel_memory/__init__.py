"""Working memory; episodic and semantic behind the Monarch seam.

Working memory is one task's structured scratchpad -- the plan, the facts an agent has
decided to keep, what it has already tried -- in Postgres (`task_memory`, migration
0008), keyed and typed, written deliberately and never as a side effect of a chat turn.

Episodic and semantic memory are interfaces only (packages/memory/AGENTS.md): the seam
in Monarch that would let them be scoped -- a visibility predicate pushed into the query,
not applied after it -- does not exist yet, and wiring to Monarch as it stands would
import exactly the post-filtering this repository refuses in retrieval. They are written
here in Citadel's vocabulary so that the eventual integration is an implementation, not
a redesign; until then they say plainly that they are not configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from citadel_platform.db import Database, Json


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
class MemoryScope:
    """Who may see a memory -- the predicate the Monarch seam must accept and push into
    its own query. Same two facts as retrieval: department and classification ceiling."""

    department: str
    classification_max: str
    actor_id: str


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


class Unconfigured:
    """Both long-term tiers until the Monarch seam exists: every call says so."""

    def remember(self, record: MemoryRecord) -> str:
        raise MemoryNotConfigured("episodic memory waits on the Monarch seam (packages/memory/AGENTS.md)")

    def recall(self, query: str, scope: MemoryScope, *, top_k: int = 5) -> Sequence[MemoryRecord]:
        raise MemoryNotConfigured("episodic memory waits on the Monarch seam (packages/memory/AGENTS.md)")

    def assert_fact(self, record: MemoryRecord) -> str:
        raise MemoryNotConfigured("semantic memory waits on the Monarch seam (packages/memory/AGENTS.md)")

    def facts(self, subject: str, scope: MemoryScope) -> Sequence[MemoryRecord]:
        raise MemoryNotConfigured("semantic memory waits on the Monarch seam (packages/memory/AGENTS.md)")


def status() -> dict[str, Optional[str]]:
    return {"working": "postgres (task_memory)", "episodic": None, "semantic": None,
            "note": "episodic and semantic tiers wait on the Monarch seam"}


__all__ = [
    "WorkingMemory",
    "MemoryScope",
    "MemoryRecord",
    "EpisodicMemory",
    "SemanticMemory",
    "Unconfigured",
    "MemoryNotConfigured",
    "status",
]
