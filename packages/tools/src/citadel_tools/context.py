"""The shapes that cross the chokepoint: what a tool is given, and what it gives back.

`ToolContext` is built by the trusted caller -- the runtime, from the task row and the
verified identity that submitted it -- and never from anything a model wrote. A model
supplies exactly two things to a tool call: the tool's name and its arguments, and both
are validated before anything runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from citadel_contracts.domain import Resource, User
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.registry import Registry
from citadel_platform.registry.schema import ToolEntry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer

from citadel_tools.policy import ActorFacts, Decision


class ToolFailure(RuntimeError):
    """The tool ran and could not do what was asked -- an honest, reportable failure
    (a missing file, a formula that divides by zero), not a policy decision."""


class ResourceNotFound(ToolFailure):
    """The concrete target does not exist (or is not a well-formed reference)."""


class ReceiptRejected(RuntimeError):
    """The executing boundary refused the receipt it was handed."""


class SandboxRunner(Protocol):
    """Where model-authored code actually executes. Two implementations: the sandbox
    service over its internal network (`citadel_sovereignty.HttpSandboxRunner`), and
    the local process sandbox (`citadel_tools.sandbox.LocalSandboxRunner`). Both
    verify the receipt themselves before running anything; the wire shape is plain
    JSON so neither side imports the other."""

    kind: str

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass
class ToolContext:
    task_id: str
    agent_id: str
    user: User
    actor: ActorFacts
    task_classification: str  # uppercase lattice level; also the actor's effective ceiling
    db: Database
    data_dir: DataDir
    registry: Registry
    registry_dir: Path
    boundary: Any  # citadel_tools.boundary.DataBoundary (typed loosely to avoid an import cycle)
    gateway: Any = None  # citadel_gateway.Gateway
    audit: Optional[AuditLog] = None
    tracer: Optional[Tracer] = None
    sandbox: Optional[SandboxRunner] = None
    progress: Optional[Callable[[Mapping[str, Any]], None]] = None
    goal: str = ""
    revision_note: Optional[str] = None

    @property
    def department(self) -> str:
        return self.actor.department

    def emit(self, **event: Any) -> None:
        if self.progress is not None:
            try:
                self.progress(event)
            except Exception:  # progress is best-effort; it never fails a tool
                pass

    def workspace(self) -> Path:
        return self.data_dir.workspace(self.task_id)


@dataclass
class Invocation:
    """One authorised call, as the executing side sees it. `receipt` is the signed
    token (None for tools that do not require one); `verified` is set only by the
    boundary that checked it, and the chokepoint refuses to return a result from a
    receipt-requiring tool whose boundary never did."""

    tool: ToolEntry
    resource: Resource
    decision: Decision
    decision_id: Optional[str] = None
    receipt: Optional[str] = None
    verified: bool = False
    verified_by: Optional[str] = None


@dataclass
class ToolOutput:
    """What a tool returns on success. `data` goes to the model (kept compact);
    `summary` is the one line a person reads in the task timeline."""

    data: dict[str, Any]
    summary: str
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool: str
    status: str  # ok | denied | invalid | not_found | error
    summary: str
    output: dict[str, Any] = field(default_factory=dict)
    decision: Optional[dict[str, Any]] = None
    receipt: Optional[dict[str, Any]] = None
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "summary": self.summary,
            "output": self.output,
            "decision": self.decision,
            "receipt": self.receipt,
            "evidence": self.evidence,
            "artifacts": self.artifacts,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }

    def for_model(self) -> dict[str, Any]:
        """The part a model sees: the outcome, never the receipt token."""
        if self.ok:
            return {"status": "ok", **self.output}
        body: dict[str, Any] = {"status": self.status, "message": self.error or self.summary}
        if self.decision and self.status == "denied":
            body["policy_rule"] = self.decision.get("rule_id")
            body["reason"] = self.decision.get("reason")
        return body


__all__ = [
    "ToolFailure",
    "ResourceNotFound",
    "ReceiptRejected",
    "SandboxRunner",
    "ToolContext",
    "Invocation",
    "ToolOutput",
    "ToolResult",
]
