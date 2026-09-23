"""Tool plugins and the single policy chokepoint.

Everything a model can do to the world goes through `Chokepoint.invoke`: resolve the
tool from registry/tools.yaml, validate its arguments, name the concrete resource,
evaluate registry/policy.yaml, record the decision, issue a receipt, dispatch -- and
the executing boundary verifies that receipt before it acts.
"""

from __future__ import annotations

from citadel_tools.boundary import DataBoundary
from citadel_tools.chokepoint import RECEIPT_TTL_S, Chokepoint, ChokepointError
from citadel_tools.context import (
    Invocation,
    ReceiptRejected,
    ResourceNotFound,
    SandboxRunner,
    ToolContext,
    ToolFailure,
    ToolOutput,
    ToolResult,
)
from citadel_tools.policy import (
    DEFAULT_DENY_REASON,
    ActorFacts,
    Decision,
    PolicyEvaluationError,
    ReceiptFacts,
    actor_facts_for_task,
    actor_facts_from_user,
    evaluate,
)
from citadel_tools.sandbox import LocalSandboxRunner, code_resource, run_verified

__all__: list[str] = [
    "ActorFacts",
    "ReceiptFacts",
    "Decision",
    "DEFAULT_DENY_REASON",
    "PolicyEvaluationError",
    "actor_facts_from_user",
    "actor_facts_for_task",
    "evaluate",
    "Chokepoint",
    "ChokepointError",
    "RECEIPT_TTL_S",
    "DataBoundary",
    "Invocation",
    "ReceiptRejected",
    "ResourceNotFound",
    "SandboxRunner",
    "ToolContext",
    "ToolFailure",
    "ToolOutput",
    "ToolResult",
    "LocalSandboxRunner",
    "code_resource",
    "run_verified",
]
