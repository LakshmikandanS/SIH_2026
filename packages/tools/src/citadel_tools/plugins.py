"""The plugin shape every tool module exports, and the resource builders they share.

`registry/tools.yaml` names each tool's `package` (a module under `citadel_tools`); that
module exports `PLUGINS`, a mapping from tool name to `ToolPlugin`. The chokepoint
discovers plugins from the manifest this way, so adding a tool is a registry entry plus
a module -- never an edit to the agent loop or to the chokepoint.

A plugin has two halves, deliberately separate:

* `resource(ctx, args)` names the concrete thing the call is about, from trusted facts
  (the task, the database) -- the policy decision and the receipt digest are about
  this, never about what the model says the thing is.
* `run(ctx, args, invocation)` does the work, and for a receipt-requiring tool first
  hands the receipt to the executing boundary with the resource recomputed at that
  moment.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from citadel_contracts.domain import Resource

from citadel_tools.context import Invocation, ToolContext, ToolOutput

ResourceFn = Callable[[ToolContext, dict[str, Any]], Resource]
RunFn = Callable[[ToolContext, dict[str, Any], Invocation], ToolOutput]


@dataclass(frozen=True)
class ToolPlugin:
    name: str
    resource: ResourceFn
    run: RunFn


def digest_of(text: str, length: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def task_resource(ctx: ToolContext, kind: str, key: str) -> Resource:
    """A resource that belongs to the task itself -- its workspace, its computations,
    its deliverables -- classified at the task's level and owned by the department of
    the person who submitted it."""
    return Resource.build(f"{kind}:{ctx.task_id}:{key}", kind, ctx.task_classification, (ctx.department,))


__all__ = ["ToolPlugin", "ResourceFn", "RunFn", "digest_of", "task_resource"]
