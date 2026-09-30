"""Design doc section 9 -- Artifact. Owned by `artifact-pipeline` (step 8).

The state-machine half of these (RELEASED is terminal, the TEMP -> RELEASED
walk) is already covered in test_foundation.py; what is exercised here is the
API-layer and Verifier behaviour step 8 builds: `app.artifact` (template,
Verifier, the generate -> verify -> request-approval pipeline) and
`app.approval` (the one transactional decision endpoint).

Following `tests/test_orchestration.py`'s own discipline: the LLM is kept out
of every test here. `app.orchestrator.service.generate_plan` is stubbed with
a hand-built, schema-conformant plan, and `rag.search`/`python.execute` are
bound to small fake backends -- but `generate_report` is always the REAL
`app.artifact.backend.generate_report_backend` (never the placeholder
`app.orchestrator.report_backend` step 7 left behind), because that backend
plus the Verifier it feeds into is exactly what this module owns and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.artifact.backend import register as register_report_backend
from app.db.engine import SessionLocal
from app.db.models import Approval, Artifact, Task
from app.db.state_machines import ApprovalState, ArtifactStatus, Classification, Role, TaskStatus
from app.identity.service import create_user
from app.main import create_app
from app.observability import get_trace
from app.orchestrator.plan import PlanGenerationResult
from app.orchestrator.schemas import OrchestratePayload, PlanModel, PlanStepModel
from app.orchestrator.service import orchestrate
from app.orchestrator.working_memory import reset as reset_memory
from app.policy.tools import Tool
from app.tool_gateway import clear_backends, register_backend

_EVIDENCE_TEXT = "2026-06-14 SCHEDULED SERVICE\n2026-02-02 UNSCHEDULED REPAIR"

_DEFAULT_EVIDENCE: list[dict[str, Any]] = [
    {
        "evidence_id": "E001",
        "document_id": "DOC-P101-HIST",
        "document_version": "1",
        "page": 1,
        "text": _EVIDENCE_TEXT,
        "classification": Classification.CONFIDENTIAL,
        "acl": ["maintenance"],
        "provenance_id": "E001",
    }
]


# ---------------------------------------------------------------------------
# Fixtures and local helpers -- mirrors tests/test_orchestration.py's own,
# with the real `app.artifact.backend` bound instead of the placeholder.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_artifact_state():
    """Working Memory and the Tool Gateway's backend registry are both
    module-level global state (see tests/test_orchestration.py's fixture of
    the same shape)."""
    reset_memory()
    clear_backends()
    yield
    reset_memory()
    clear_backends()


@pytest.fixture
def engineer_and_approver(db):
    """One engineer (submits tasks) and one approver (decides on artifacts)
    -- section 3's own `roles: engineer | approver | admin`."""
    engineer = create_user(
        db,
        username="j.rao",
        password="engineer-pw",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
        user_id="U123",
    )
    approver = create_user(
        db,
        username="a.singh",
        password="approver-pw",
        roles=[Role.APPROVER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
        user_id="U-APPROVER",
    )
    db.commit()
    return engineer, approver


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _good_plan(task_id: str) -> PlanModel:
    """The same fixed three-step shape design doc section 5.2 specifies --
    `python.execute`'s `code` is the planner's own hallucinated placeholder
    on purpose (`app.orchestrator.think` overwrites it; never executed)."""
    return PlanModel(
        plan_id="P000TEST8",
        task_id=task_id,
        steps=[
            PlanStepModel(
                step_id="S1",
                agent_type="researcher",
                action=Tool.RAG_SEARCH,
                arguments={"query": "Pump P-101 maintenance history"},
            ),
            PlanStepModel(
                step_id="S2",
                agent_type="researcher",
                action=Tool.PYTHON_EXECUTE,
                arguments={"code": "from search_engine import search"},
            ),
            PlanStepModel(
                step_id="S3",
                agent_type="writer",
                action=Tool.GENERATE_REPORT,
                arguments={"template": "maintenance_summary_v1"},
            ),
        ],
    )


def _fake_python_execute(request: Any) -> dict[str, Any]:
    """A real, sandboxless stand-in for the Execution Service -- runs
    whatever code THINK actually built (never the planner's placeholder)."""
    code = request.arguments["code"]
    assert "search_engine" not in code
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})
    return {"stdout": buf.getvalue(), "stderr": "", "exit_code": 0}


def _register_fake_backends(evidence: Optional[list[Mapping[str, Any]]] = None) -> None:
    """`rag.search`/`python.execute` are lightweight fakes (no Ollama, no
    Docker needed); `generate_report` is always the REAL
    `app.artifact.backend` -- this module's own deliverable, exercised for
    real in every test below."""
    rows = list(evidence) if evidence is not None else list(_DEFAULT_EVIDENCE)

    def _fake_rag_search(request: Any) -> dict[str, Any]:
        return {"results": rows}

    register_backend(Tool.RAG_SEARCH, _fake_rag_search)
    register_backend(Tool.PYTHON_EXECUTE, _fake_python_execute)
    register_report_backend()


def _stub_generate_plan(monkeypatch) -> None:
    import app.orchestrator.service as service_module

    def _fake(task_id: str, classification: str) -> PlanGenerationResult:
        return PlanGenerationResult(
            plan=_good_plan(task_id),
            model_id="qwen3-local",
            routing_reason="local_confidential_reasoning",
            repaired=False,
        )

    monkeypatch.setattr(service_module, "generate_plan", _fake)


def _submit_verified_task(
    client: TestClient,
    engineer_headers: dict[str, str],
    monkeypatch,
    *,
    evidence: Optional[list[Mapping[str, Any]]] = None,
) -> tuple[str, str, str]:
    """Drive a real task from `/task` through to WAITING_FOR_APPROVAL with a
    VERIFIED artifact and a REVIEW_REQUIRED approval -- section 1.1 steps
    2-15, minus the live model/sandbox calls. Returns
    `(task_id, artifact_id, approval_id)`."""
    _register_fake_backends(evidence)
    _stub_generate_plan(monkeypatch)

    response = client.post(
        "/task",
        json={
            "text": (
                "Using the available internal maintenance documents, identify the "
                "recent maintenance history of Pump P-101 and generate a short "
                "maintenance summary report."
            ),
            "classification": Classification.CONFIDENTIAL,
        },
        headers=engineer_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == TaskStatus.WAITING_FOR_APPROVAL, body
    task_id = body["task_id"]
    artifact_id = body["artifact_id"]
    assert artifact_id is not None

    with SessionLocal() as session:
        approval = session.execute(
            select(Approval).where(Approval.artifact_id == artifact_id)
        ).scalar_one()
        approval_id = approval.approval_id

    return task_id, artifact_id, approval_id


# ---------------------------------------------------------------------------
# section 9's three checklist lines
# ---------------------------------------------------------------------------


def test_generate_verify_approve_release_happy_path(db, engineer_and_approver, monkeypatch):
    """Generate -> verify -> approve -> release, full happy path."""
    engineer, approver = engineer_and_approver
    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    task_id, artifact_id, approval_id = _submit_verified_task(client, engineer_headers, monkeypatch)

    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact.status == ArtifactStatus.VERIFIED
        assert artifact.hash is not None
        assert Path(artifact.path).exists()
        approval = session.get(Approval, approval_id)
        assert approval.state == ApprovalState.REVIEW_REQUIRED

    trace = client.get(f"/tasks/{task_id}/trace", headers=engineer_headers).json()
    event_types = [e["event_type"] for e in trace["events"]]
    assert "ARTIFACT_CREATED" in event_types
    assert "ARTIFACT_VERIFIED" in event_types
    assert "APPROVAL_REQUESTED" in event_types
    # order implied by section 4's mapping table
    assert (
        event_types.index("ARTIFACT_CREATED")
        < event_types.index("ARTIFACT_VERIFIED")
        < event_types.index("APPROVAL_REQUESTED")
    )

    # The five checks, printed via the ARTIFACT_VERIFIED payload.
    verified_event = next(e for e in trace["events"] if e["event_type"] == "ARTIFACT_VERIFIED")
    checks = verified_event["payload"]
    assert checks["file_exists_and_readable"] is True
    assert checks["sha256_computed"] is True
    assert checks["has_required_sections"] is True
    assert checks["evidence_classification_ok"] is True
    assert checks["has_provenance"] is True
    assert checks["passed"] is True

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "APPROVED", "comment": "Looks correct."},
        headers=approver_headers,
    )
    assert decision.status_code == 200, decision.text
    decision_body = decision.json()
    assert decision_body["artifact_status"] == ArtifactStatus.RELEASED
    assert decision_body["task_status"] == TaskStatus.COMPLETED

    task_resp = client.get(f"/tasks/{task_id}", headers=engineer_headers).json()
    assert task_resp["status"] == TaskStatus.COMPLETED

    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact.status == ArtifactStatus.RELEASED
        approval = session.get(Approval, approval_id)
        assert approval.state == ApprovalState.APPROVED
        assert approval.decision == ApprovalState.APPROVED
        assert approval.approver_id == approver.user_id
        assert approval.comment == "Looks correct."

    final_trace = client.get(f"/tasks/{task_id}/trace", headers=engineer_headers).json()
    final_types = [e["event_type"] for e in final_trace["events"]]
    assert final_types.index("APPROVAL_GRANTED") < final_types.index("ARTIFACT_RELEASED")


def test_released_artifact_rejects_further_mutation(db, engineer_and_approver, monkeypatch):
    """A RELEASED artifact rejects any further mutation attempt -- and this
    proves BB-039's explicit `if artifact.status == RELEASED: reject()` line
    itself fires, not merely the (also-true) "approval already decided"
    check: the second Approval row below genuinely is REVIEW_REQUIRED, so
    only the artifact-status check can be what refuses it."""
    engineer, approver = engineer_and_approver
    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    task_id, artifact_id, approval_id = _submit_verified_task(client, engineer_headers, monkeypatch)

    approve = client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "APPROVED"}, headers=approver_headers
    )
    assert approve.status_code == 200, approve.text

    with SessionLocal() as session:
        second_approval = Approval(
            approval_id="APR-SECOND",
            artifact_id=artifact_id,
            state=ApprovalState.REVIEW_REQUIRED,
        )
        session.add(second_approval)
        session.commit()

    mutate_approve = client.post(
        "/approvals/APR-SECOND/decision", json={"decision": "APPROVED"}, headers=approver_headers
    )
    assert mutate_approve.status_code == 409
    assert mutate_approve.json()["detail"]["error"]["code"] == "ARTIFACT_ALREADY_RELEASED"

    mutate_reject = client.post(
        "/approvals/APR-SECOND/decision", json={"decision": "REJECTED"}, headers=approver_headers
    )
    assert mutate_reject.status_code == 409
    assert mutate_reject.json()["detail"]["error"]["code"] == "ARTIFACT_ALREADY_RELEASED"

    # The ordinary path too: retrying the original (already-decided)
    # approval is refused, for a different, also-correct reason.
    retry_original = client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "APPROVED"}, headers=approver_headers
    )
    assert retry_original.status_code == 409
    assert retry_original.json()["detail"]["error"]["code"] == "APPROVAL_NOT_REVIEWABLE"

    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact.status == ArtifactStatus.RELEASED  # unchanged by any attempt above


def test_verification_failure_marks_the_task_failed(db, task, monkeypatch):
    """A verification failure marks the task FAILED (no silent pass): empty
    evidence means `generate_report`'s own `provenance` list is empty, which
    fails the Verifier's fifth check (`len(artifact.provenance) > 0`) on its
    own -- the other four pass, isolating exactly which check trips."""
    _register_fake_backends(evidence=[])
    _stub_generate_plan(monkeypatch)

    payload = OrchestratePayload(
        task_id=task.task_id,
        user_id=task.user_id,
        classification=task.classification,
        task_type="DOCUMENT_ANALYSIS",
        requirements=task.requirements,
    )
    result = orchestrate(payload)

    assert result.status == TaskStatus.FAILED
    assert result.agent_status == "FAILED"
    assert "verification" in (result.reason or "").lower()
    assert "'has_provenance': False" in (result.reason or "")

    row = db.get(Task, task.task_id)
    db.refresh(row)
    assert row.status == TaskStatus.FAILED

    with SessionLocal() as session:
        artifacts = session.execute(
            select(Artifact).where(Artifact.task_id == task.task_id)
        ).scalars().all()
        assert len(artifacts) == 1
        artifact = artifacts[0]
        # No artifact failure state exists (BB-038) -- a failed verification
        # leaves the row at TEMP, never CANDIDATE or VERIFIED.
        assert artifact.status == ArtifactStatus.TEMP
        assert artifact.hash is None

        approvals = session.execute(
            select(Approval).where(Approval.artifact_id == artifact.artifact_id)
        ).scalars().all()
        assert approvals == []  # no REVIEW_REQUIRED Approval row was ever created


# ---------------------------------------------------------------------------
# Approval propagation (BB-047) -- identity, atomicity, and the reject/revise
# hand-off to the Orchestrator's one scoped case (section 5.3)
# ---------------------------------------------------------------------------


def test_approver_id_always_comes_from_the_session_never_the_body(db, engineer_and_approver, monkeypatch):
    """section 6.4's non-negotiable rule, applied here "with no exception"
    (section 6.10's own wording): an engineer cannot decide at all (role
    gate), and an approver's spoofed `approver_id`/`user_id` in the body
    changes nothing about who is recorded."""
    engineer, approver = engineer_and_approver
    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    task_id, artifact_id, approval_id = _submit_verified_task(client, engineer_headers, monkeypatch)

    forbidden = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "APPROVED", "approver_id": approver.user_id},
        headers=engineer_headers,
    )
    assert forbidden.status_code == 403

    spoofed = client.post(
        f"/approvals/{approval_id}/decision",
        json={
            "decision": "APPROVED",
            "comment": "Looks correct.",
            "approver_id": "U-SOMEONE-ELSE",
            "user_id": "U-SOMEONE-ELSE",
        },
        headers=approver_headers,
    )
    assert spoofed.status_code == 200, spoofed.text

    with SessionLocal() as session:
        approval = session.get(Approval, approval_id)
        assert approval.approver_id == approver.user_id
        assert approval.approver_id != "U-SOMEONE-ELSE"


def test_approval_decision_is_one_transaction_all_or_nothing(db, engineer_and_approver, monkeypatch):
    """§6.10: "in ONE transaction" -- forcing a failure between the Artifact
    and Task transitions must leave Approval, Artifact, and Task all at
    their pre-call states, not partially advanced."""
    engineer, approver = engineer_and_approver
    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    task_id, artifact_id, approval_id = _submit_verified_task(client, engineer_headers, monkeypatch)

    import app.approval.router as approval_router_module

    def _boom(session, task, new_status, expected_version=None):
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr(approval_router_module, "transition_task", _boom)

    with pytest.raises(RuntimeError):
        client.post(
            f"/approvals/{approval_id}/decision", json={"decision": "APPROVED"}, headers=approver_headers
        )

    with SessionLocal() as session:
        approval = session.get(Approval, approval_id)
        assert approval.state == ApprovalState.REVIEW_REQUIRED
        assert approval.approver_id is None
        artifact = session.get(Artifact, artifact_id)
        assert artifact.status == ArtifactStatus.VERIFIED
        task_row = session.get(Task, task_id)
        assert task_row.status == TaskStatus.WAITING_FOR_APPROVAL

    events = get_trace(task_id)
    assert not any(e.event_type == "APPROVAL_GRANTED" for e in events)
    assert not any(e.event_type == "ARTIFACT_RELEASED" for e in events)


def test_rejected_then_revised_then_second_rejection_fails_the_task(db, engineer_and_approver, monkeypatch):
    """REJECTED -> the Orchestrator's one scoped revision (section 5.3) runs
    -> a second REJECTED on the revised artifact ends the task FAILED --
    mirroring `tests/test_orchestration.py::
    test_revision_reject_then_regenerate_then_fail_on_second_rejection`, but
    triggered the real way: through this endpoint, not by calling
    `app.orchestrator.revision.revise_report` directly."""
    engineer, approver = engineer_and_approver
    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    task_id, artifact_id, approval_id = _submit_verified_task(client, engineer_headers, monkeypatch)

    first_reject = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "REJECTED", "comment": "Please clarify the vibration units."},
        headers=approver_headers,
    )
    assert first_reject.status_code == 200, first_reject.text
    first_body = first_reject.json()
    assert first_body["task_status"] == TaskStatus.WAITING_FOR_APPROVAL
    revised_artifact_id = first_body["revision"]["artifact_id"]
    assert revised_artifact_id is not None
    assert revised_artifact_id != artifact_id

    with SessionLocal() as session:
        original_approval = session.get(Approval, approval_id)
        assert original_approval.state == ApprovalState.REJECTED

        revised_artifact = session.get(Artifact, revised_artifact_id)
        assert revised_artifact.status == ArtifactStatus.VERIFIED
        assert "vibration units" in Path(revised_artifact.path).read_text(encoding="utf-8")

        second_approval = session.execute(
            select(Approval).where(Approval.artifact_id == revised_artifact_id)
        ).scalar_one()
        assert second_approval.state == ApprovalState.REVIEW_REQUIRED
        second_approval_id = second_approval.approval_id

    second_reject = client.post(
        f"/approvals/{second_approval_id}/decision",
        json={"decision": "REJECTED", "comment": "Still not acceptable."},
        headers=approver_headers,
    )
    assert second_reject.status_code == 200, second_reject.text
    second_body = second_reject.json()
    assert second_body["task_status"] == TaskStatus.FAILED
    assert second_body["revision"] is None

    task_resp = client.get(f"/tasks/{task_id}", headers=engineer_headers).json()
    assert task_resp["status"] == TaskStatus.FAILED
