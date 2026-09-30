"""Task, Agent, Evidence, Artifact, Approval, User -- as plain schemas.

Design doc §3 defines these as wire-shaped domain objects. Today they exist
two different ways depending on who needs them: `app/db/models.py` gives each
one (except Evidence) a SQLAlchemy ORM row, and `app/policy/context.py` gives
Task/Agent/User a second, narrower projection (`PolicyTask`/`PolicyAgent`/
`PolicyUser`) because "the Policy Engine must be a pure function of its
inputs [and] a row could lazily load a relationship mid-decision" (that
module's own docstring). Evidence has no ORM row at all -- design doc §6.9
calls it Data-Plane-owned and deliberately not given storage -- and lives
today as a plain dataclass in `app/rag/evidence.py`.

`contracts/` needs a version of these six that depends on nothing -- no
SQLAlchemy, no `app/` -- because a Decision Receipt has to be checked by a
service that may not hold a database session at all (Target Architecture §2:
"every executing service verifies... for itself"). These are that version:
the plain-schema shape of §3, ported field-for-field from `app/db/models.py`
and `app/rag/evidence.py` (P1 §1: "not a rename for tidiness"). They are not
a replacement for the ORM rows -- `app/db/models.py` still owns persistence
-- they are what crosses a boundary that has no database on the other side.

Two things deliberately did NOT come along:

  * `User.password_hash` -- Identity-internal, never part of the wire object
    even in the original ("a User returned over the wire never carries it").
  * The relationships (`Task.agent`, `Artifact.approvals`, ...) -- an ORM
    convenience with no plain-schema equivalent; a caller that needs the
    related object fetches it by id.

## `Resource` -- the one addition

`Resource` is not one of design doc §3's six named objects, but it has to be
here anyway: it is `app/policy/context.py::PolicyResource`, moved rather than
copied, because P1's `resource_digest()` (`contracts/receipts.py`) and the
existing Policy Engine (`app/policy/engine.py`) must compute over *exactly*
the same four fields or the receipt and the policy decision can silently
drift apart -- precisely the failure C-002 exists to prevent. `PolicyResource`
was already the general "thing a decision is about" concept, not a
policy-specific one; moving it here just makes that true structurally.

`PolicyResource.for_task()` did not come with it. It builds a resource from a
`PolicyTask`, whose synthesized `.department` field (assembled from the
owning User at decision time -- see that class's own docstring) has no
counterpart on the plain `Task` below, where `department` is, correctly, not
a Task field at all. Wiring `Resource` into the policy/capability path,
including what replaces `for_task()`, is P1 step 3's job
(`decide_and_issue`), not this file's -- this step only relocates the shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class User:
    """Design doc §3 User, minus `password_hash` (see module docstring)."""

    user_id: str
    username: str
    roles: tuple[str, ...] = ()
    clearance: str = ""
    department: str = ""
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Task:
    """Design doc §3 Task. `department` is deliberately absent -- it is an
    attribute of the owning User, not the Task (see `PolicyTask` in
    `app/policy/context.py`, whose docstring records this as a gap in the
    frozen doc, resolved there rather than by adding a column here)."""

    task_id: str
    user_id: str
    classification: str
    status: str
    requirements: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Agent:
    """Design doc §3 Agent. Exactly one per task (BB-014) -- enforced by the
    `uq_agents_one_per_task` constraint on the ORM row, not by this shape."""

    agent_id: str
    task_id: str
    agent_type: str
    status: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Artifact:
    """Design doc §3 Artifact."""

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
    """Design doc §3 Approval. `state` is the §4 state machine
    (NOT_REQUIRED -> REVIEW_REQUIRED -> APPROVED/REJECTED, see
    `state_machines.APPROVAL`); `decision` is this field, null until a human
    decides. The two agree in the terminal states by construction -- the
    endpoint that sets them is the one place that does so, in one
    transaction, exactly as design doc §6.10 requires; this is only the
    shape, not that guarantee."""

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
    """Design doc §3 / §6.9 Evidence, ported from `app/rag/evidence.py`.

    `Evidence.from_chunk` did not come with it: it bridges from
    `app.rag.store.Chunk`, an internal Data Plane type this package cannot
    depend on. That constructor stays in `app/rag/evidence.py`; this is only
    the shape that crosses the boundary.
    """

    evidence_id: str
    document_id: str
    document_version: str
    page: int
    text: str
    classification: str
    acl: tuple[str, ...]
    provenance_id: str
    #: Not part of design doc §3's schema -- an extra transparency field so a
    #: result list's ranking is inspectable. Omitted from `to_dict()` when
    #: absent, exactly as the original.
    score: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """The design doc §3 / §6.9 wire shape, exactly."""
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
        return payload


@dataclass(frozen=True)
class Resource:
    """The concrete thing a decision is about -- moved from
    `app/policy/context.py::PolicyResource` (see module docstring for why).

    This is the whole reason capability and policy/receipt are separate
    checks (design doc §6.6, P1 §0): a capability proves "this agent may
    attempt `rag.search` in general"; a `Resource` is what lets a decision --
    and, from P1 onward, a receipt's digest -- answer "is THIS document
    allowed right now". Built by the trusted caller from the concrete
    target, never from agent-supplied arguments.
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
