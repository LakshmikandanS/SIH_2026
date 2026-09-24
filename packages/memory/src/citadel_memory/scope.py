"""Who is asking, for memory -- the same two facts retrieval's predicate needs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryScope:
    """Built by the trusted caller from a verified identity (capped at the task's
    classification when there is a task), never from anything a model wrote. It is
    what the memory store's SQL filters by, in the same statement as the search."""

    department: str
    classification_max: str
    actor_id: str


__all__ = ["MemoryScope"]
