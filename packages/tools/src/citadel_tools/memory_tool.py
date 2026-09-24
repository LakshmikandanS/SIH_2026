"""memory.recall: what the workbench remembers, through the chokepoint like any read.

The memory manager (citadel_memory) filters memories by the caller's department and
classification ceiling in the same SQL statement as the search -- here, the ceiling is
the task's classification, so a task can never recall what it could not have read.
Memories come back as context (M1, M2...), not as citable evidence: what earlier work
concluded tells an agent where to look; the documents are still what it cites.
"""

from __future__ import annotations

from typing import Any

from citadel_contracts.domain import Resource
from citadel_memory import MemoryManager, MemoryScope

from citadel_tools.context import Invocation, ToolContext, ToolOutput
from citadel_tools.plugins import ToolPlugin, digest_of


def memory_scope(ctx: ToolContext) -> MemoryScope:
    return MemoryScope(department=ctx.department, classification_max=ctx.actor.classification_max,
                       actor_id=ctx.user.user_id)


def _resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    key = digest_of(f"{args['query']}|{args.get('top_k')}")
    return Resource.build(f"memory-recall:{ctx.task_id}:{key}", "memory", ctx.actor.classification_max, (ctx.department,))


def _recall(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    manager = MemoryManager(ctx.db, ctx.gateway, ctx.audit)
    memories = manager.recall(str(args["query"]), memory_scope(ctx), top_k=int(args.get("top_k") or 5),
                              task_id=ctx.task_id)
    data: dict[str, Any] = {
        "memories": [
            {"ref": m.ref, "tier": m.tier, "type": m.memory_type, "subject": m.subject, "content": m.content,
             "certainty": m.certainty, "remembered_on": m.created_at[:10]}
            for m in memories
        ],
        "about": ("What earlier tasks concluded, recalled by meaning and recency. Use it to decide where to look "
                  "and what to check; cite documents (E ids), not memories, for facts."),
    }
    if not memories:
        data["about"] = "Nothing is remembered about this yet."
    return ToolOutput(
        data=data,
        summary=f"{len(memories)} memory item(s) recalled" if memories else "nothing remembered about this",
        detail={"memories": [m.to_dict() for m in memories]},
    )


PLUGINS = {"memory.recall": ToolPlugin("memory.recall", _resource, _recall)}

__all__ = ["PLUGINS", "memory_scope"]
