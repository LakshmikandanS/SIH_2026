"""Generate -> verify -> request approval, wired together (design doc section
4's mapping table, rows 1-3 and 6).

    | Task    | Artifact  | Approval      | Trigger                          |
    | RUNNING | TEMP      | NOT_REQUIRED  | Report generated, not yet checked|
    | RUNNING | CANDIDATE | NOT_REQUIRED  | Verifier begins checking         |
    | WAITING_FOR_APPROVAL | VERIFIED | REVIEW_REQUIRED | All 5 checks pass  |
    | RUNNING (-> FAILED) | TEMP (rejected) | NOT_REQUIRED | Verifier fails  |

`create_and_verify_artifact` is the one function that walks all of this,
called synchronously right after `generate_report`'s Tool Gateway backend
returns (design doc section 6.10: "called synchronously right after
generation" -- there is no network hop and no LLM call between rendering the
file and checking it). It replaces the artifact-creation half of
`app.orchestrator.agent_loop._commit_artifact`, which is now a thin
delegator to this module -- see that module's own comment for why touching
it was unavoidable (the Verifier has to run "right after generation", and
that seam is the OBSERVATION half of the `generate_report` step, nowhere
else).

Two separate commits, deliberately:

  1. The `Artifact` row at TEMP, plus `ARTIFACT_CREATED` -- this really did
     happen (a file was rendered, a row exists) regardless of what the
     Verifier finds next, so it is not rolled back on a verification
     failure.
  2. Verification + (on pass) the CANDIDATE -> VERIFIED walk, the new
     `Approval` row at REVIEW_REQUIRED, `ARTIFACT_VERIFIED` then
     `APPROVAL_REQUESTED` -- one atomic commit. On failure this second
     transaction is rolled back in full, which is what "a failed
     verification leaves the artifact at TEMP" (`app/db/state_machines.py`'s
     own Artifact comment, BB-038) means concretely: CANDIDATE is a
     same-transaction stepping stone to VERIFIED, never a state a failed
     check leaves committed on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import func, select

from app import ids
from app.db.engine import SessionLocal
from app.db.models import Approval, Artifact, Task
from app.db.state_machines import ApprovalState, ArtifactStatus
from app.db.transitions import transition_approval, transition_artifact
from app.observability import EventType, append_event
from app.artifact.verifier import VerifiableArtifact, VerificationReport, run_verification

#: `Artifact.type` for this slice's one document kind (design doc section 3's
#: own example value). Not a tool name (the grep in
#: `tests/test_security.py::test_no_code_path_invokes_a_tool_outside_the_gateway`
#: only cares about `app.policy.tools.Tool` members), so a literal here is fine.
ARTIFACT_TYPE = "maintenance_summary_report"


@dataclass(frozen=True)
class ArtifactPipelineOutcome:
    artifact_id: str
    artifact_path: Optional[str]
    verified: bool
    failure_reason: Optional[str]
    approval_id: Optional[str]
    report: VerificationReport


def _create_temp_artifact(task_id: str, result: Mapping[str, Any]) -> Artifact:
    """Row 1 of the mapping table: TEMP, `ARTIFACT_CREATED`, its own commit."""
    with SessionLocal() as session:
        existing_count = session.execute(
            select(func.count()).select_from(Artifact).where(Artifact.task_id == task_id)
        ).scalar_one()
        artifact = Artifact(
            artifact_id=result["artifact_id"],
            task_id=task_id,
            version=existing_count + 1,
            type=ARTIFACT_TYPE,
            status=ArtifactStatus.TEMP,
            path=result.get("path"),
            provenance=list(result.get("provenance", [])),
        )
        session.add(artifact)
        session.flush()
        append_event(
            task_id,
            None,
            EventType.ARTIFACT_CREATED,
            {
                "artifact_id": artifact.artifact_id,
                "version": artifact.version,
                "path": artifact.path,
                # section 3's own domain model: "hash: null, // populated
                # once VERIFIED" -- the Verifier writes it, not this step.
                "hash": None,
                "provenance": artifact.provenance,
                "revised": bool(result.get("revised", False)),
            },
            session=session,
        )
        session.commit()
        session.refresh(artifact)
        session.expunge(artifact)
        return artifact


def _verify_and_request_approval(
    task_id: str, artifact_id: str, evidence: list[Mapping[str, Any]]
) -> tuple[bool, VerificationReport, Optional[str]]:
    """Rows 2-3 (pass) or row 6 (fail) of the mapping table, one transaction.

    Returns `(passed, report, approval_id)`. On failure `approval_id` is
    `None` and nothing beyond the already-committed TEMP row exists for this
    artifact -- BB-038: "any failure ⇒ task FAILED for this slice, no
    auto-revision", enforced by the caller (`create_and_verify_artifact`),
    not by an artifact-level failure state (there isn't one).
    """
    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        task = session.get(Task, task_id)
        if artifact is None or task is None:
            raise LookupError(f"artifact {artifact_id!r} or task {task_id!r} not found")

        adapter = VerifiableArtifact(artifact=artifact, task=task, evidence=evidence)
        report = run_verification(adapter)

        if not report.passed:
            session.rollback()
            return False, report, None

        # CANDIDATE -> VERIFIED, contiguously, in this one transaction --
        # CANDIDATE ("Verifier begins checking", row 2) is never independently
        # observable outside of it.
        transition_artifact(session, artifact, ArtifactStatus.CANDIDATE)
        transition_artifact(session, artifact, ArtifactStatus.VERIFIED)
        append_event(
            task_id,
            None,
            EventType.ARTIFACT_VERIFIED,
            {"artifact_id": artifact.artifact_id, **report.as_dict()},
            session=session,
        )

        approval_id = ids.new_id(ids.APPROVAL)
        approval = Approval(
            approval_id=approval_id,
            artifact_id=artifact.artifact_id,
            state=ApprovalState.NOT_REQUIRED,
        )
        session.add(approval)
        session.flush()
        transition_approval(session, approval, ApprovalState.REVIEW_REQUIRED)
        append_event(
            task_id,
            None,
            EventType.APPROVAL_REQUESTED,
            {"approval_id": approval_id, "artifact_id": artifact.artifact_id},
            session=session,
        )

        session.commit()
        return True, report, approval_id


def create_and_verify_artifact(
    *, task_id: str, result: Mapping[str, Any], evidence: list[Mapping[str, Any]]
) -> ArtifactPipelineOutcome:
    """The whole of section 6.10's generate -> verify -> request-approval
    walk, for one `generate_report` call's `result` (the Tool Gateway
    backend's return value -- `artifact_id`, `path`, `provenance`, ...).

    `evidence` is the Working Memory evidence list at the moment
    `generate_report` ran (section 5.1's "given the step's action + current
    evidence" -- the fourth Verifier check needs each cited row's
    `classification`, which is never persisted on the `Artifact` row itself,
    only on the in-flight evidence dicts, per section 6.9's Working Memory).
    """
    artifact = _create_temp_artifact(task_id, result)
    passed, report, approval_id = _verify_and_request_approval(task_id, artifact.artifact_id, evidence)

    if passed:
        return ArtifactPipelineOutcome(
            artifact_id=artifact.artifact_id,
            artifact_path=artifact.path,
            verified=True,
            failure_reason=None,
            approval_id=approval_id,
            report=report,
        )

    reason = (
        f"artifact {artifact.artifact_id!r} failed verification "
        f"(BB-038, no auto-revision): {report.as_dict()}"
    )
    return ArtifactPipelineOutcome(
        artifact_id=artifact.artifact_id,
        artifact_path=artifact.path,
        verified=False,
        failure_reason=reason,
        approval_id=None,
        report=report,
    )
