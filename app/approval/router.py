"""`POST /approvals/{approval_id}/decision` -- the one transactional endpoint
(design doc section 6.10, BB-047).

    "An approval decision that never actually propagated to release
     anything [...] On APPROVED, in one transaction: Approval.decision =
     APPROVED, Artifact.status = RELEASED, Task.status = COMPLETED; emits
     APPROVAL_GRANTED then ARTIFACT_RELEASED. On REJECTED: Approval.decision
     = REJECTED, Task.status = REVISION_REQUIRED momentarily, then the
     Orchestrator's scoped revision (section 5.3) runs."

`approver_id` comes from the verified session JWT
(`app.identity.dependencies.current_identity`) and nowhere else -- section
6.4's non-negotiable rule, applied here "with no exception" per that
section's own wording. `DecisionRequest` below has no `approver_id` field at
all, so there is no code path from the request body to the acting approver's
identity; a client that sends one anyway has it silently ignored (pydantic's
default "extra fields are dropped" behaviour), which is exactly section
6.4's "never trusted and is ignored if present", not a rejection.

Immutability (BB-039) is enforced here, at the API layer, not the storage
layer -- `if artifact.status == ArtifactStatus.RELEASED: reject()` appears
explicitly in both `_approve` and `_reject` below, as its own visible line,
rather than relying solely on the state-machine backstop
(`app.db.transitions.transition_artifact` already refuses any transition out
of RELEASED, since it is terminal in `app.db.state_machines.ARTIFACT`). A
true write-once store is out of scope for this MVP; this is a documented
limitation, not an oversight.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.db.base import utcnow
from app.db.engine import SessionLocal
from app.db.models import Agent, Approval, Artifact, Task
from app.db.state_machines import (
    ApprovalState,
    ArtifactStatus,
    IllegalTransition,
    Role,
    TaskStatus,
)
from app.db.transitions import (
    VersionConflict,
    transition_approval,
    transition_artifact,
    transition_task,
)
from app.identity.dependencies import require_role
from app.identity.tokens import SessionIdentity
from app.observability import EventType, append_event
from app.orchestrator.revision import RevisionNotPermitted, revise_report

router = APIRouter(tags=["approval"])


class DecisionRequest(BaseModel):
    """section 6.10's request body, exactly `{"decision": ..., "comment": ...}`.

    No `approver_id` field exists here at all -- see this module's own
    docstring for why that absence, not a runtime check, is what makes
    "never from the request body" true.
    """

    decision: str
    comment: Optional[str] = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in (ApprovalState.APPROVED, ApprovalState.REJECTED):
            raise ValueError(
                f"decision must be {ApprovalState.APPROVED!r} or "
                f"{ApprovalState.REJECTED!r}, got {value!r}"
            )
        return value


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


def _load_approval_and_artifact(session, approval_id: str) -> tuple[Approval, Artifact, Task]:
    """The lookups + guards common to both branches: the approval must exist
    and be reviewable, and the artifact it points at must not already be
    RELEASED (BB-039's explicit API-layer check, checked here so neither
    `_approve` nor `_reject` can skip it)."""
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_APPROVAL", f"no approval {approval_id!r}")
    if approval.state != ApprovalState.REVIEW_REQUIRED:
        raise _error(
            status.HTTP_409_CONFLICT,
            "APPROVAL_NOT_REVIEWABLE",
            f"approval {approval_id!r} is {approval.state!r}, not REVIEW_REQUIRED",
        )

    artifact = session.get(Artifact, approval.artifact_id)
    if artifact is None:
        raise _error(
            status.HTTP_404_NOT_FOUND, "UNKNOWN_ARTIFACT", f"no artifact {approval.artifact_id!r}"
        )

    # BB-039 -- the explicit API-layer immutability check, not just the
    # state-machine backstop. This is what the design doc calls out by name:
    # "if artifact.status == RELEASED: reject()".
    if artifact.status == ArtifactStatus.RELEASED:
        raise _error(
            status.HTTP_409_CONFLICT,
            "ARTIFACT_ALREADY_RELEASED",
            f"artifact {artifact.artifact_id!r} is RELEASED; no further mutation is permitted",
        )

    task = session.get(Task, artifact.task_id)
    if task is None:
        raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_TASK", f"no task {artifact.task_id!r}")

    return approval, artifact, task


def _approve(approval_id: str, approver_id: str, comment: Optional[str]) -> dict[str, Any]:
    with SessionLocal() as session:
        approval, artifact, task = _load_approval_and_artifact(session, approval_id)

        approval.approver_id = approver_id
        approval.comment = comment
        approval.timestamp = utcnow()

        try:
            # section 6.10: "in ONE transaction" -- Approval, Artifact
            # (VERIFIED -> APPROVED -> RELEASED, its two remaining linear
            # steps), and Task all move here, committed together below.
            transition_approval(session, approval, ApprovalState.APPROVED)
            transition_artifact(session, artifact, ArtifactStatus.APPROVED)
            transition_artifact(session, artifact, ArtifactStatus.RELEASED)
            transition_task(session, task, TaskStatus.COMPLETED, expected_version=task.version)
        except (IllegalTransition, VersionConflict) as exc:
            session.rollback()
            raise _error(status.HTTP_409_CONFLICT, "INVALID_STATE_TRANSITION", str(exc)) from None

        # Order matters (design doc's events list + section 4 mapping table):
        # APPROVAL_GRANTED first, then ARTIFACT_RELEASED.
        append_event(
            task.task_id,
            approver_id,
            EventType.APPROVAL_GRANTED,
            {"approval_id": approval_id, "artifact_id": artifact.artifact_id, "comment": comment},
            session=session,
        )
        append_event(
            task.task_id,
            approver_id,
            EventType.ARTIFACT_RELEASED,
            {"artifact_id": artifact.artifact_id, "task_id": task.task_id},
            session=session,
        )

        session.commit()

        return {
            "approval_id": approval_id,
            "decision": ApprovalState.APPROVED,
            "artifact_id": artifact.artifact_id,
            "artifact_status": artifact.status,
            "task_id": task.task_id,
            "task_status": task.status,
        }


def _reject(approval_id: str, approver_id: str, comment: Optional[str]) -> dict[str, Any]:
    with SessionLocal() as session:
        approval, artifact, task = _load_approval_and_artifact(session, approval_id)

        agent = session.execute(
            select(Agent).where(Agent.task_id == task.task_id)
        ).scalar_one_or_none()
        if agent is None:
            raise _error(
                status.HTTP_409_CONFLICT,
                "NO_AGENT_FOR_TASK",
                f"task {task.task_id!r} has no Agent row (section 3, BB-014)",
            )

        approval.approver_id = approver_id
        approval.comment = comment
        approval.timestamp = utcnow()

        try:
            transition_approval(session, approval, ApprovalState.REJECTED)
        except IllegalTransition as exc:
            session.rollback()
            raise _error(status.HTTP_409_CONFLICT, "INVALID_STATE_TRANSITION", str(exc)) from None

        append_event(
            task.task_id,
            approver_id,
            EventType.APPROVAL_REJECTED,
            {"approval_id": approval_id, "artifact_id": artifact.artifact_id, "comment": comment},
            session=session,
        )

        session.commit()
        task_id, agent_id = task.task_id, agent.agent_id

    # section 5.3's one scoped revision, triggered here -- this endpoint does
    # NOT regenerate the report itself. `revise_report` (already built by
    # `orchestrator`, step 7) owns the momentary
    # WAITING_FOR_APPROVAL -> REVISION_REQUIRED -> RUNNING walk, re-runs
    # `generate_report` once with this comment appended, and ends the task
    # FAILED itself on a second rejection (`RevisionNotPermitted`, caught
    # below only to shape the HTTP response -- the task is already FAILED by
    # the time that exception reaches here).
    try:
        outcome = revise_report(task_id, agent_id, comment or "")
    except RevisionNotPermitted as exc:
        return {
            "approval_id": approval_id,
            "decision": ApprovalState.REJECTED,
            "task_id": task_id,
            "task_status": TaskStatus.FAILED,
            "revision": None,
            "reason": str(exc),
        }

    return {
        "approval_id": approval_id,
        "decision": ApprovalState.REJECTED,
        "task_id": task_id,
        "task_status": outcome.status,
        "revision": {"artifact_id": outcome.artifact_id, "reason": outcome.reason},
    }


@router.post("/approvals/{approval_id}/decision")
def post_approval_decision(
    approval_id: str,
    payload: DecisionRequest,
    identity: SessionIdentity = Depends(require_role(Role.APPROVER)),
) -> dict[str, Any]:
    """section 6.10's one transactional decision endpoint. Role-gated the
    same way `POST /admin/tools/{tool_name}/disable` is (§6.8):
    `require_role` reads the session's roles from the verified JWT, so an
    engineer cannot approve their own report by asserting `"roles":
    ["approver"]` in the body any more than they could self-grant admin
    there.
    """
    approver_id = identity.user_id  # section 6.4 -- never `payload`, which has no such field
    if payload.decision == ApprovalState.APPROVED:
        return _approve(approval_id, approver_id, payload.comment)
    return _reject(approval_id, approver_id, payload.comment)
