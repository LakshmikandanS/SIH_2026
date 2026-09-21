"""Task, Agent, Evidence, Artifact, Approval, User -- as plain schemas.

Ported field-for-field from the prototype's `contracts/domain.py`
(`packages/contracts/AGENTS.md` porting table: "mostly" -- port, with the
two changes recorded below). Zero database, zero ORM: a Decision Receipt has
to be checked by a service that may not hold a database session at all, and
these are the shape that crosses a boundary that has no database on the
other side. They are not a replacement for whatever `citadel_platform`'s own
persistence layer uses internally.

Two things did not come along, exactly as in the prototype:

  * `User.password_hash` -- identity-internal, never part of the wire object.
  * The relationships (`Task.agent`, `Artifact.approvals`, ...) -- an ORM
    convenience with no plain-schema equivalent; a caller that needs the
    related object fetches it by id.

## Two changes on the way in

**`Agent` drops the one-per-task assumption.** The prototype pinned this
with a `uq_agents_one_per_task` uniqueness constraint on its ORM row. This
repo's own root `AGENTS.md` names exactly that pattern as a failure mode by
example: a scaling assumption baked into a schema constraint instead of left
to the orchestrator. Nothing here, and nothing planned in `citadel_runtime`,
assumes a task has at most one `Agent` row -- a task with a researcher agent
and a writer agent both live is a normal state, not a constraint violation
waiting to be added later.

**`Evidence` gains `bbox`.** `packages/knowledge/AGENTS.md`'s citation
contract is explicit: "Document id, version, page, bounding box... a
citation that cannot be pointed at is not a citation." The prototype's
`Evidence` (ported from its `app/rag/evidence.py`) predates that contract
and has no such field. Added here, optional, normalised `(x0, y0, x1, y1)`
in `[0, 1]` of the page image so it survives independent of render
resolution, and omitted from `to_dict()` when absent -- the same convention
already used for `score` -- so a plain-text ingested document (page, but no
meaningful region to highlight) is not forced to fabricate one.

## `Resource` -- the one addition

`Resource` is not one of the six named schemas above, but it belongs here
anyway: it is the general "thing a decision is about" concept, and
`resource_digest()` (`receipts.py`) and the policy evaluator
(`citadel_tools`) must compute over *exactly* the same four fields or a
receipt and a policy decision can silently drift apart. `Resource` moving
here (rather than living beside just one of them) is what keeps that true
structurally rather than by convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class User:
    """A person. Minus `password_hash` (see module docstring)."""

    user_id: str
    username: str
    roles: tuple[str, ...] = ()
    clearance: str = ""
    department: str = ""
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Task:
    """A unit of agentic work. `department` is deliberately absent -- it is
    an attribute of the owning User, not the Task."""

    task_id: str
    user_id: str
    classification: str
    status: str
    requirements: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Agent:
    """One executing agent, working on one task.

    See the module docstring: the prototype's `uq_agents_one_per_task`
    constraint is a named failure mode here and is not carried forward.
    Two Agent rows sharing a `task_id` is valid.
    """

    agent_id: str
    task_id: str
    agent_type: str
    status: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Artifact:
    """A generated deliverable."""

    artifact_id: str
    task_id: str
    version: int
    type: str
    status: str
    hash: Optional[str] = None
    provenance: tuple[str, ...] = ()
    path: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Approval:
    """A human decision on an artifact. `state` is the
    `state_machines.APPROVAL` machine (NOT_REQUIRED -> REVIEW_REQUIRED ->
    APPROVED/REJECTED); `decision` is this field, null until a human
    decides. The two must agree in the terminal states by construction --
    the endpoint that sets them is the one place that does so, in one
    transaction; this is only the shape, not that guarantee."""

    approval_id: str
    artifact_id: str
    approver_id: Optional[str] = None
    state: str = "NOT_REQUIRED"
    decision: Optional[str] = None
    comment: Optional[str] = None
    timestamp: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Evidence:
    """A retrieved, cited chunk of a source document.

    `bbox` is new relative to the prototype -- see the module docstring.
    Everything else is ported unchanged.
    """

    evidence_id: str
    document_id: str
    document_version: str
    page: int
    text: str
    classification: str
    acl: tuple[str, ...]
    provenance_id: str
    #: Not part of the wire shape's required fields -- an extra transparency
    #: field so a result list's ranking is inspectable. Omitted from
    #: `to_dict()` when absent.
    score: Optional[float] = None
    #: (x0, y0, x1, y1) normalised to [0, 1] of the page image -- enough to
    #: highlight the exact region in the UI (packages/knowledge/AGENTS.md).
    #: Optional: a plain-text ingested document has a page but no layout
    #: region to point at. Omitted from `to_dict()` when absent, exactly
    #: like `score`.
    bbox: Optional[tuple[float, float, float, float]] = None

    def to_dict(self) -> dict[str, Any]:
        """The wire shape. `score` and `bbox` appear only when populated."""
        payload: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "page": self.page,
            "text": self.text,
            "classification": self.classification,
            "acl": list(self.acl),
            "provenance_id": self.provenance_id,
        }
        if self.score is not None:
            payload["score"] = round(self.score, 6)
        if self.bbox is not None:
            payload["bbox"] = list(self.bbox)
        return payload


@dataclass(frozen=True)
class Resource:
    """The concrete thing a decision is about (see module docstring).

    This is the whole reason capability and policy/receipt are separate
    checks: a capability proves "this agent may attempt `rag.search` in
    general"; a `Resource` is what lets a decision -- and its receipt's
    digest -- answer "is THIS document allowed right now". Built by the
    trusted caller from the concrete target, never from agent-supplied
    arguments.
    """

    resource_id: str
    type: str
    classification: str
    acl: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        resource_id: str,
        type: str,
        classification: str,
        acl: Sequence[str],
    ) -> "Resource":
        return cls(
            resource_id=resource_id,
            type=type,
            classification=classification,
            acl=tuple(acl),
        )


__all__ = ["User", "Task", "Agent", "Artifact", "Approval", "Evidence", "Resource"]
