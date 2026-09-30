"""Design doc section 9 -- Orchestration (BB-001, BB-002, BB-003, section
6.2, section 6.3). Owned by `orchestrator` (step 7).

Per the mission brief for this step: the LLM is kept out of most tests here
-- a test that calls `hermes3` is slow (7-13s, docs/BUILD_LOG.md) and
non-deterministic. Logic tests stub `app.orchestrator.plan.generate_plan` /
`app.orchestrator.plan.generate_json` and use fake Tool Gateway backends;
`TestClient` tests exercise the real HTTP surface with the same stubs. The
small number of genuinely live-model tests are marked `@_needs_ollama` and
skip cleanly if Ollama is unreachable.
"""

from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db.models import Agent, Artifact, Task
from app.db.state_machines import AgentStatus, Classification, Role, TaskStatus
from app.model_router.errors import MODEL_CLASSIFICATION_INCOMPATIBLE, ModelRoutingError
from app.model_router.manifest import ModelManifestEntry
from app.model_router.router import route_models
from app.observability import get_trace, verify_chain
from app.orchestrator import errors as orch_errors
from app.orchestrator.agent_loop import run_agent_loop
from app.orchestrator.plan import PlanGenerationFailed, PlanGenerationResult, generate_plan
from app.orchestrator.report_backend import register as register_report_backend
from app.orchestrator.revision import RevisionNotPermitted, revise_report
from app.orchestrator.schemas import OrchestratePayload, PlanModel, PlanStepModel
from app.orchestrator.service import orchestrate
from app.orchestrator.working_memory import get_or_create as get_memory
from app.orchestrator.working_memory import reset as reset_memory
from app.policy.tools import Tool
from app.tool_gateway import clear_backends, register_backend

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent(db, task):
    """One Agent per task (section 3, BB-014) -- same shape as
    `tests/test_security.py`/`tests/test_rag.py`'s fixture of the same name."""
    row = Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher")
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _clean_orchestrator_state():
    """Working Memory (`app.orchestrator.working_memory`) and the Tool
    Gateway's backend registry are both module-level global state -- reset
    both before and after every test in this module, matching
    `tests/test_execution.py`/`tests/test_rag.py`'s own use of
    `clear_backends()`."""
    reset_memory()
    clear_backends()
    yield
    reset_memory()
    clear_backends()


def _good_plan(task_id: str) -> PlanModel:
    """A hand-built, schema-conformant plan -- the shape design doc section
    5.2 fixes, used everywhere a test needs *a* valid plan without paying for
    a real model call. The `python.execute` step's `code` is deliberately the
    same kind of unusable placeholder BUILD_LOG.md found the real model
    producing (`from search_engine import search`), so every test built on
    this fixture also proves that placeholder is never executed."""
    return PlanModel(
        plan_id="P000TEST",
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


_EVIDENCE_TEXT = "2026-06-14 SCHEDULED SERVICE\n2026-02-02 UNSCHEDULED REPAIR"


def _fake_rag_search(request) -> dict:
    return {
        "results": [
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
    }


def _fake_python_execute(request) -> dict:
    """A real, sandboxless stand-in for the Execution Service -- runs
    whatever code the agent loop's THINK step actually sent, exactly as the
    real Execution Service would (design doc section 1.1 step 12's own
    shape), so a test using this fixture still proves the S2 code came from
    real evidence and not the planner's own hallucinated placeholder."""
    code = request.arguments["code"]
    assert "search_engine" not in code, (
        "the planner's hallucinated S2 code must never reach a tool backend -- "
        "app.orchestrator.think overwrites it before ACTION"
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})
    return {"stdout": buf.getvalue(), "stderr": "", "exit_code": 0}


def _register_fake_backends() -> None:
    register_backend(Tool.RAG_SEARCH, _fake_rag_search)
    register_backend(Tool.PYTHON_EXECUTE, _fake_python_execute)
    register_report_backend()


def _stub_generate_plan(monkeypatch, plan: PlanModel) -> None:
    """Patch `app.orchestrator.service.generate_plan` (the name imported
    into that module's namespace) so `orchestrate()` never calls the LLM."""
    import app.orchestrator.service as service_module

    def _fake(task_id: str, classification: str) -> PlanGenerationResult:
        return PlanGenerationResult(
            plan=plan.model_copy(update={"task_id": task_id}),
            model_id="qwen3-local",
            routing_reason="local_confidential_reasoning",
            repaired=False,
        )

    monkeypatch.setattr(service_module, "generate_plan", _fake)


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


_needs_ollama = pytest.mark.skipif(
    not _ollama_reachable(), reason="Ollama is not reachable at localhost:11434"
)


# ---------------------------------------------------------------------------
# section 9's four checklist lines
# ---------------------------------------------------------------------------


def test_plan_is_schema_conformant_json():
    """Task -> plan produces valid, schema-conformant JSON."""
    good = {
        "plan_id": "P1",
        "task_id": "T1",
        "steps": [
            {
                "step_id": "S1",
                "agent_type": "researcher",
                "action": Tool.RAG_SEARCH,
                "arguments": {"query": "Pump P-101 maintenance history"},
            },
            {
                "step_id": "S2",
                "agent_type": "researcher",
                "action": Tool.PYTHON_EXECUTE,
                "arguments": {"code": "print(1)"},
            },
            {
                "step_id": "S3",
                "agent_type": "writer",
                "action": Tool.GENERATE_REPORT,
                "arguments": {"template": "maintenance_summary_v1"},
            },
        ],
    }
    plan = PlanModel.model_validate(good)
    assert plan.plan_id == "P1"
    assert [s.action for s in plan.steps] == [
        Tool.RAG_SEARCH,
        Tool.PYTHON_EXECUTE,
        Tool.GENERATE_REPORT,
    ]

    # The actual JSON Schema section 5.2 asks the response be validated
    # against -- produced by pydantic, not hand-written.
    schema = plan.model_json_schema()
    assert schema["type"] == "object"
    assert "steps" in schema["properties"]

    # Out-of-order steps: schema-conformant shape, wrong scenario shape.
    from app.orchestrator.schemas import validate_fixed_shape

    reordered = PlanModel.model_validate({**good, "steps": list(reversed(good["steps"]))})
    with pytest.raises(ValueError):
        validate_fixed_shape(reordered)

    # A fourth, unknown action is rejected by the schema itself.
    with pytest.raises(ValidationError):
        PlanModel.model_validate(
            {
                "plan_id": "P2",
                "task_id": "T1",
                "steps": [
                    {"step_id": "S1", "agent_type": "researcher", "action": "host.shell", "arguments": {}}
                ],
            }
        )


def test_every_plan_step_reaches_the_tool_gateway(db, task, agent, monkeypatch):
    """Each plan step's action reaches the Tool Gateway, never a tool
    directly -- both structurally (the agent loop imports no tool backend
    module) and behaviourally (every step's call is visible in the audit
    trail with a CAPABILITY_CHECKED + TOOL_EXECUTED pair)."""
    # Structural: app/orchestrator/agent_loop.py never imports app.rag,
    # app.execution, or execution_service -- the only two real backend
    # packages plus the isolated zone's own package name.
    source = (_REPO_ROOT / "app" / "orchestrator" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_prefixes = ("app.rag", "app.execution", "execution_service")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(banned_prefixes):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(banned_prefixes):
                offenders.append(node.module)
    assert offenders == [], (
        f"agent_loop.py imports a tool backend module directly: {offenders} -- "
        "every ACTION must route through app.tool_gateway.invoke instead"
    )

    # Behavioural: run the real loop against fake backends, bound only
    # through register_backend (never called by agent_loop.py itself), and
    # check each of the three steps produced the Tool Gateway's own events.
    _register_fake_backends()
    plan = _good_plan(task.task_id)
    result = run_agent_loop(task_id=task.task_id, agent_id=agent.agent_id, plan=plan)
    assert result.outcome == AgentStatus.SUCCESS

    events = get_trace(task.task_id)
    capability_checks = [e for e in events if e.event_type == "CAPABILITY_CHECKED"]
    tool_executions = [e for e in events if e.event_type == "TOOL_EXECUTED"]
    assert len(capability_checks) == 3
    assert len(tool_executions) == 3
    # One fresh capability per step (§6.5) -- never the same token reused.
    capability_ids = {e.payload["capability_id"] for e in capability_checks}
    assert len(capability_ids) == 3


def test_observation_feeds_the_next_think(db, task, agent):
    """Observation feeds correctly into the next step's THINK -- S1's
    retrieved evidence is what S2's generated code actually parses, not the
    planner's own hallucinated placeholder."""
    _register_fake_backends()
    plan = _good_plan(task.task_id)
    result = run_agent_loop(task_id=task.task_id, agent_id=agent.agent_id, plan=plan)

    assert result.outcome == AgentStatus.SUCCESS
    assert result.memory is not None
    # S1's observation landed in Working Memory...
    assert result.memory.evidence and result.memory.evidence[0]["evidence_id"] == "E001"
    # ...and S2's THINK step used exactly that text, computing the real
    # answer (data/README.md: most recent service is 2026-06-14).
    assert result.memory.computed == {
        "most_recent": "2026-06-14",
        "days_since": (result.memory.computed or {}).get("days_since"),
        "records_found": 2,
    }
    # The S2 action actually sent to the Tool Gateway must contain S1's
    # evidence text and must NOT contain the planner's hallucinated import.
    s2_trace = next(t for t in result.steps if t.step_id == "S2")
    assert "2026-06-14" in s2_trace.arguments["code"]
    assert "search_engine" not in s2_trace.arguments["code"]


def test_agent_terminates_on_success_failed_and_max_steps(db, task, agent):
    """Agent terminates on SUCCESS, FAILED, and MAX_STEPS correctly."""
    # SUCCESS
    _register_fake_backends()
    plan = _good_plan(task.task_id)
    success = run_agent_loop(task_id=task.task_id, agent_id=agent.agent_id, plan=plan)
    assert success.outcome == AgentStatus.SUCCESS
    assert success.memory.artifact_id is not None

    # FAILED -- a backend that always errors, exhausting the one retry
    # ("RETRY (same step, <=1 retry)", section 5.1).
    reset_memory()
    clear_backends()
    call_count = {"n": 0}

    def _always_fails(request):
        call_count["n"] += 1
        raise RuntimeError("simulated backend failure")

    register_backend(Tool.RAG_SEARCH, _always_fails)
    one_step_plan = PlanModel(
        plan_id="P-FAIL",
        task_id=task.task_id,
        steps=[
            PlanStepModel(
                step_id="S1", agent_type="researcher", action=Tool.RAG_SEARCH, arguments={"query": "x"}
            )
        ],
    )
    failed = run_agent_loop(task_id=task.task_id, agent_id=agent.agent_id, plan=one_step_plan)
    assert failed.outcome == AgentStatus.FAILED
    assert call_count["n"] == 2  # one original attempt + exactly one retry

    # MAX_STEPS -- a plan longer than the step budget.
    reset_memory()
    clear_backends()
    register_backend(Tool.RAG_SEARCH, lambda request: {"results": []})
    long_plan = PlanModel(
        plan_id="P-LONG",
        task_id=task.task_id,
        steps=[
            PlanStepModel(
                step_id=f"S{i}", agent_type="researcher", action=Tool.RAG_SEARCH, arguments={"query": "x"}
            )
            for i in range(1, 9)
        ],
    )
    max_steps_result = run_agent_loop(
        task_id=task.task_id, agent_id=agent.agent_id, plan=long_plan, max_steps=3
    )
    assert max_steps_result.outcome == AgentStatus.MAX_STEPS
    assert len(max_steps_result.steps) == 3


# ---------------------------------------------------------------------------
# The repair-prompt path (section 5.2)
# ---------------------------------------------------------------------------


def test_plan_repair_prompt_recovers_from_one_invalid_output(db, task, monkeypatch):
    """The model's first structured-output call returns invalid JSON; the
    one repair prompt then succeeds. `repaired=True` is how the caller (and
    the PLAN_CREATED event) can tell this happened."""
    import app.orchestrator.plan as plan_module

    calls = {"n": 0}
    good_raw = json.dumps(
        {
            "plan_id": "PX",
            "task_id": task.task_id,
            "steps": [
                {
                    "step_id": "S1",
                    "agent_type": "researcher",
                    "action": Tool.RAG_SEARCH,
                    "arguments": {"query": "Pump P-101 maintenance history"},
                },
                {
                    "step_id": "S2",
                    "agent_type": "researcher",
                    "action": Tool.PYTHON_EXECUTE,
                    "arguments": {"code": "print(1)"},
                },
                {
                    "step_id": "S3",
                    "agent_type": "writer",
                    "action": Tool.GENERATE_REPORT,
                    "arguments": {"template": "maintenance_summary_v1"},
                },
            ],
        }
    )

    def _fake_generate_json(*, model, prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "this is not JSON at all {"
        return good_raw

    monkeypatch.setattr(plan_module, "generate_json", _fake_generate_json)

    result = generate_plan(task.task_id, task.classification)
    assert calls["n"] == 2
    assert result.repaired is True
    assert [s.action for s in result.plan.steps] == [
        Tool.RAG_SEARCH,
        Tool.PYTHON_EXECUTE,
        Tool.GENERATE_REPORT,
    ]


def test_plan_generation_fails_after_two_invalid_outputs(db, task, monkeypatch):
    """Two consecutive schema-invalid outputs (the original call, then the
    one repair) -- `PlanGenerationFailed`, no further retry."""
    import app.orchestrator.plan as plan_module

    calls = {"n": 0}

    def _fake_generate_json(*, model, prompt, **kwargs):
        calls["n"] += 1
        return "still not JSON {{{"

    monkeypatch.setattr(plan_module, "generate_json", _fake_generate_json)

    with pytest.raises(PlanGenerationFailed):
        generate_plan(task.task_id, task.classification)
    assert calls["n"] == 2  # the original call plus exactly one repair, no more


# ---------------------------------------------------------------------------
# Model Router routing failure (section 6.3, BB-007)
# ---------------------------------------------------------------------------


def test_model_classification_incompatible_routing_failure():
    """A task whose classification exceeds a model's max_classification
    fails routing outright with MODEL_CLASSIFICATION_INCOMPATIBLE."""
    incompatible_manifest = {
        "qwen3-local": ModelManifestEntry(
            model_id="qwen3-local",
            capability="reasoning",
            max_classification=Classification.INTERNAL,
            ollama_model="hermes3",
        ),
        "bge-base-local": ModelManifestEntry(
            model_id="bge-base-local",
            capability="embedding",
            max_classification=Classification.CONFIDENTIAL,
            ollama_model="nomic-embed-text",
        ),
    }
    with pytest.raises(ModelRoutingError) as excinfo:
        route_models(
            task_id="T1",
            required_capabilities=["reasoning"],
            classification=Classification.CONFIDENTIAL,
            manifest=incompatible_manifest,
        )
    assert excinfo.value.code == MODEL_CLASSIFICATION_INCOMPATIBLE


def test_model_routing_failure_fails_the_task(db, task, monkeypatch):
    """The same failure, end to end: `orchestrate()` marks the task FAILED
    rather than raising a bare exception out of `app.model_router`."""
    import app.orchestrator.plan as plan_module

    def _always_incompatible(*, task_id, required_capabilities, classification):
        raise ModelRoutingError(
            MODEL_CLASSIFICATION_INCOMPATIBLE, "task classification exceeds the model's ceiling"
        )

    monkeypatch.setattr(plan_module, "route_models", _always_incompatible)

    payload = OrchestratePayload(
        task_id=task.task_id,
        user_id=task.user_id,
        classification=task.classification,
        task_type="DOCUMENT_ANALYSIS",
        requirements=task.requirements,
    )
    result = orchestrate(payload)
    assert result.status == TaskStatus.FAILED
    assert MODEL_CLASSIFICATION_INCOMPATIBLE in (result.reason or "")

    with db.no_autoflush:
        row = db.get(Task, task.task_id)
        db.refresh(row)
        assert row.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# §6.2 handoff errors
# ---------------------------------------------------------------------------


def test_orchestrate_rejects_a_task_that_is_already_running(db, task, monkeypatch):
    _register_fake_backends()
    _stub_generate_plan(monkeypatch, _good_plan(task.task_id))
    payload = OrchestratePayload(
        task_id=task.task_id,
        user_id=task.user_id,
        classification=task.classification,
        task_type="DOCUMENT_ANALYSIS",
        requirements=task.requirements,
    )
    first = orchestrate(payload)
    assert first.status == TaskStatus.WAITING_FOR_APPROVAL

    with pytest.raises(orch_errors.OrchestrationError) as excinfo:
        orchestrate(payload)
    assert excinfo.value.code == orch_errors.TASK_ALREADY_RUNNING


def test_orchestrate_rejects_invalid_requirements(db, task):
    payload = OrchestratePayload(
        task_id=task.task_id,
        user_id=task.user_id,
        classification=task.classification,
        task_type="DOCUMENT_ANALYSIS",
        requirements={"needs_rag": True, "needs_document_generation": False},
    )
    with pytest.raises(orch_errors.OrchestrationError) as excinfo:
        orchestrate(payload)
    assert excinfo.value.code == orch_errors.INVALID_REQUIREMENTS


# ---------------------------------------------------------------------------
# End-to-end task run (function-level and HTTP-level)
# ---------------------------------------------------------------------------


def test_end_to_end_task_run_reaches_waiting_for_approval(db, task, monkeypatch):
    """A full `orchestrate()` call, stubbed plan generation, real fake
    tool-gateway backends: SUCCESS -> WAITING_FOR_APPROVAL, an Artifact row
    created, and an unbroken hash chain covering the whole run."""
    _register_fake_backends()
    _stub_generate_plan(monkeypatch, _good_plan(task.task_id))

    payload = OrchestratePayload(
        task_id=task.task_id,
        user_id=task.user_id,
        classification=task.classification,
        task_type="DOCUMENT_ANALYSIS",
        requirements=task.requirements,
    )
    result = orchestrate(payload)

    assert result.status == TaskStatus.WAITING_FOR_APPROVAL
    assert result.agent_status == AgentStatus.SUCCESS
    assert result.artifact_id is not None

    row = db.get(Task, task.task_id)
    db.refresh(row)
    assert row.status == TaskStatus.WAITING_FOR_APPROVAL

    artifact = db.get(Artifact, result.artifact_id)
    assert artifact is not None
    assert artifact.task_id == task.task_id
    assert Path(artifact.path).exists()

    events = get_trace(task.task_id)
    event_types = {e.event_type for e in events}
    for required in (
        "TASK_CREATED" if False else "PLAN_CREATED",  # TASK_CREATED is emitted by /task, not orchestrate()
        "AGENT_STARTED",
        "ACTION_REQUESTED",
        "STATE_COMMITTED",
    ):
        assert required in event_types
    assert verify_chain() is True


def test_task_endpoint_end_to_end(monkeypatch):
    """`POST /task` (the Query Router role) -> the real `/internal/orchestrate`
    handoff, in-process -> `GET /tasks/{id}` and `GET /tasks/{id}/trace`,
    both served only by the Orchestrator (section 6.2, C-005)."""
    from app.db import init_db
    from app.db.engine import SessionLocal
    from app.db.models import User
    from app.identity.service import create_user
    from app.main import create_app

    init_db.reset()
    reset_memory()
    clear_backends()
    with SessionLocal() as session:
        create_user(
            session,
            username="j.rao",
            password="correct horse battery staple",
            roles=[Role.ENGINEER],
            clearance=Classification.CONFIDENTIAL,
            department="maintenance",
            user_id="U123",
        )
        session.commit()

    _register_fake_backends()

    app = create_app()
    client = TestClient(app)

    login = client.post("/login", json={"username": "j.rao", "password": "correct horse battery staple"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    def _fake_generate_plan(task_id: str, classification: str) -> PlanGenerationResult:
        return PlanGenerationResult(
            plan=_good_plan(task_id),
            model_id="qwen3-local",
            routing_reason="local_confidential_reasoning",
            repaired=False,
        )

    import app.orchestrator.service as service_module

    monkeypatch.setattr(service_module, "generate_plan", _fake_generate_plan)

    response = client.post(
        "/task",
        json={
            "text": "Using the available internal maintenance documents, identify the "
            "recent maintenance history of Pump P-101 and generate a short "
            "maintenance summary report.",
            "classification": "CONFIDENTIAL",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == TaskStatus.WAITING_FOR_APPROVAL
    task_id = body["task_id"]

    status_response = client.get(f"/tasks/{task_id}", headers=headers)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == TaskStatus.WAITING_FOR_APPROVAL

    trace_response = client.get(f"/tasks/{task_id}/trace", headers=headers)
    assert trace_response.status_code == 200
    trace_body = trace_response.json()
    assert trace_body["events"][0]["event_type"] == "TASK_CREATED"
    event_types = {e["event_type"] for e in trace_body["events"]}
    assert {"PLAN_CREATED", "AGENT_STARTED", "ACTION_REQUESTED", "STATE_COMMITTED"} <= event_types

    clear_backends()


# ---------------------------------------------------------------------------
# section 5.3 -- the one scoped plan-revision case
# ---------------------------------------------------------------------------


def test_revision_reject_then_regenerate_then_fail_on_second_rejection(db, task, agent):
    """Reject -> regenerate once -> FAILED on second rejection. Evidence and
    events from the completed steps are kept untouched; only
    `generate_report` re-runs."""
    register_report_backend()

    # Simulate the original run having already happened: Working Memory
    # populated (as S1/S2 would have left it) and Task at WAITING_FOR_APPROVAL
    # with one existing report artifact (as S3 would have left it).
    memory = get_memory(task.task_id)
    memory.evidence = [
        {
            "evidence_id": "E001",
            "document_id": "DOC-P101-HIST",
            "page": 1,
            "text": _EVIDENCE_TEXT,
            # step 8 integration fix: real evidence rows always carry
            # `classification` (§3's own Evidence schema) -- the Verifier's
            # fourth check (`app.artifact.verifier`) needs it on every row
            # `revise_report`'s regenerated artifact cites, now that
            # `app.orchestrator.agent_loop._commit_artifact` runs the real
            # Verifier instead of just writing a TEMP row.
            "classification": Classification.CONFIDENTIAL,
        }
    ]
    memory.computed = {"most_recent": "2026-06-14", "days_since": 85, "records_found": 2}

    from app.db.state_machines import ArtifactStatus

    db.add(
        Artifact(
            artifact_id="ART_ORIGINAL",
            task_id=task.task_id,
            version=1,
            type="maintenance_summary_report",
            status=ArtifactStatus.TEMP,
            path="var/artifacts/original.md",
            provenance=["E001"],
        )
    )
    task.status = TaskStatus.WAITING_FOR_APPROVAL
    db.add(task)
    db.commit()

    first = revise_report(task.task_id, agent.agent_id, "Please clarify the vibration units.")
    assert first.status == TaskStatus.WAITING_FOR_APPROVAL
    assert first.artifact_id is not None
    assert first.artifact_id != "ART_ORIGINAL"

    revised_artifact = db.get(Artifact, first.artifact_id)
    assert revised_artifact is not None
    assert "vibration units" in Path(revised_artifact.path).read_text(encoding="utf-8")

    row = db.get(Task, task.task_id)
    db.refresh(row)
    assert row.status == TaskStatus.WAITING_FOR_APPROVAL

    # Original evidence/events untouched: Working Memory still has S1's
    # evidence (rule 1) -- nothing re-fetched it.
    assert memory.evidence[0]["evidence_id"] == "E001"

    # Second rejection: the one revision has already been used.
    with pytest.raises(RevisionNotPermitted):
        revise_report(task.task_id, agent.agent_id, "Still not acceptable.")

    row2 = db.get(Task, task.task_id)
    db.refresh(row2)
    assert row2.status == TaskStatus.FAILED
