"""Design doc section 9 -- Audit.

    [x] Every event type in section 6.12's list is actually emitted at least
        once during the happy-path run
    [x] /trace shows the denial-path event (TOOL_DENIED) and the
        emergency-control event, not only the happy path
    [x] The hash chain is unbroken end to end for one full task run

The hash-chain third is fully testable after step 3 (foundation-schema) and is
covered here. The first two needed a real run through the Orchestrator and the
CLI and were marked skip until steps 7 and 9 landed; `cli` (step 9) now
implements both below, using the same fake-backend/stubbed-planner discipline
`tests/test_artifact.py`/`tests/test_cli.py` use (no Ollama, no Docker
required) plus one direct Tool Gateway denial and one reject/re-approve cycle
so a single scenario's trace legitimately touches every one of the 16 types.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.artifact.backend import register as register_report_backend
from app.db.models import Event
from app.db.state_machines import Classification, Role, TaskStatus
from app.identity.service import create_user
from app.main import create_app
from app.observability import (
    ALL_EVENT_TYPES,
    ChainBroken,
    EventType,
    UnknownEventType,
    append_event,
    format_trace,
    get_trace,
    verify_chain,
)
from app.observability import hashing
from app.observability.writer import EventWriter
from app.orchestrator.plan import PlanGenerationResult
from app.orchestrator.schemas import PlanModel, PlanStepModel
from app.policy.tools import Tool
from app.tool_gateway import clear_backends, register_backend


# --------------------------------------------------------------------------
# Hash chain -- section 9 Audit, third checkbox
# --------------------------------------------------------------------------


def test_chain_is_unbroken_across_a_run_of_events(db, task):
    for event_type in ALL_EVENT_TYPES:
        append_event(task.task_id, "A123", event_type, {"note": event_type.lower()})

    events = get_trace(task.task_id)
    assert len(events) == len(ALL_EVENT_TYPES)
    assert verify_chain(task.task_id) is True


def test_each_event_links_to_its_predecessor(db, task):
    for i in range(5):
        append_event(task.task_id, "A123", EventType.ACTION_REQUESTED, {"i": i})

    events = get_trace(task.task_id)
    for previous, current in zip(events, events[1:]):
        assert current.previous_hash == previous.hash


def test_first_event_links_to_the_genesis_hash(db, task):
    from app import config

    event = append_event(task.task_id, None, EventType.TASK_CREATED, {})
    assert event.previous_hash == config.GENESIS_HASH
    assert event.event_id == "EVT0001"
    assert event.seq == 1


def test_event_ids_are_sequential_in_the_doc_format(db, task):
    for i in range(3):
        append_event(task.task_id, "A123", EventType.TOOL_EXECUTED, {"i": i})
    assert [e.event_id for e in get_trace(task.task_id)] == [
        "EVT0001",
        "EVT0002",
        "EVT0003",
    ]


def test_tampering_with_a_payload_breaks_the_chain(db, task):
    append_event(task.task_id, "A123", EventType.TOOL_DENIED, {"reason": "POLICY_DENIED"})
    append_event(task.task_id, "A123", EventType.STATE_COMMITTED, {})

    # Rewrite history directly in the table, bypassing the writer.
    row = db.execute(select(Event).where(Event.seq == 1)).scalar_one()
    row.payload = {"reason": "ALLOWED"}
    db.commit()

    with pytest.raises(ChainBroken, match="tampered content"):
        verify_chain(task.task_id)


def test_tampering_with_an_event_type_breaks_the_chain(db, task):
    """The demo's central claim is that a denial is recorded as a denial.
    Relabelling TOOL_DENIED as TOOL_EXECUTED must break the chain."""
    append_event(task.task_id, "A123", EventType.TOOL_DENIED, {"reason": "POLICY_DENIED"})
    append_event(task.task_id, "A123", EventType.STATE_COMMITTED, {})

    row = db.execute(select(Event).where(Event.seq == 1)).scalar_one()
    row.event_type = EventType.TOOL_EXECUTED
    db.commit()

    with pytest.raises(ChainBroken, match="tampered content"):
        verify_chain(task.task_id)


def test_deleting_an_event_breaks_the_chain(db, task):
    for i in range(3):
        append_event(task.task_id, "A123", EventType.ACTION_REQUESTED, {"i": i})

    row = db.execute(select(Event).where(Event.seq == 2)).scalar_one()
    db.delete(row)
    db.commit()

    with pytest.raises(ChainBroken, match="sequence gap"):
        verify_chain(task.task_id)


def test_chain_spans_tasks_and_system_scoped_events(db, task):
    """An admin DISABLE TOOL has no task_id; it must still be in the one
    chain, and a per-task trace must not be mistaken for the whole chain."""
    append_event(task.task_id, "A123", EventType.TASK_CREATED, {})
    append_event(None, "U-admin", EventType.POLICY_DECISION, {"tool_disabled": True})
    append_event(task.task_id, "A123", EventType.TOOL_DENIED, {})

    assert len(get_trace(task.task_id)) == 2
    assert len(get_trace(None)) == 3
    assert verify_chain(task.task_id) is True


# --------------------------------------------------------------------------
# Writer contract -- foundation-schema's own "Done when"
# --------------------------------------------------------------------------


def test_writer_rejects_an_event_type_outside_the_vocabulary(db, task):
    with pytest.raises(UnknownEventType):
        append_event(task.task_id, "A123", "TOOL_MAYBE_ALLOWED", {})


def test_event_can_be_enlisted_in_a_callers_transaction(db, task):
    """Section 6.10 needs Approval + Artifact + Task + two events in one
    commit; a rollback must take the events with it."""
    session = db
    append_event(task.task_id, "A123", EventType.APPROVAL_GRANTED, {}, session=session)
    session.rollback()
    assert get_trace(task.task_id) == []


def test_append_event_is_the_only_writer_to_the_event_table():
    """Grep the application for direct Event inserts. Only writer.py may
    construct an Event row -- foundation-schema's "Done when" asks for exactly
    one such code path, so this asserts it rather than trusting convention."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # `Event(` as a constructor call, not `class Event(Base)` and not an
    # annotation like `-> Event:` or `list[Event]`.
    construction = re.compile(r"(?<!class )(?<![\w.\[])Event\(")

    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel == "app/observability/writer.py":
            continue  # the one sanctioned writer
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if construction.search(line) and not stripped.startswith("class Event"):
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], f"direct Event construction outside the writer: {offenders}"


# --------------------------------------------------------------------------
# Hash strategy seam
# --------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_name", hashing.available())
def test_every_hash_strategy_produces_a_verifiable_chain(db, strategy_name):
    """The strategy is swappable (config.EVENT_HASH_STRATEGY); whichever is
    selected must write and verify a consistent chain."""
    from app.observability import writer as writer_module

    previous = writer_module.set_writer(EventWriter(strategy_name=strategy_name))
    try:
        append_event("T123", "A123", EventType.TASK_CREATED, {"s": strategy_name})
        append_event("T123", "A123", EventType.TOOL_EXECUTED, {"s": strategy_name})
        assert verify_chain("T123") is True
    finally:
        writer_module.set_writer(previous)


def test_literal_formula_does_not_bind_event_type_but_default_does():
    """Documents exactly what the default strategy buys over the doc-literal
    one, so the choice stays visible rather than folklore."""
    record_denied = {
        "event_id": "EVT0001",
        "task_id": "T123",
        "actor_id": "A123",
        "event_type": "TOOL_DENIED",
        "payload": {"reason": "POLICY_DENIED"},
        "timestamp": "2026-09-06T10:00:00+00:00",
    }
    record_executed = dict(record_denied, event_type="TOOL_EXECUTED")
    prev = "0" * 64

    literal = hashing.get_strategy("payload_only_v1")
    canonical = hashing.get_strategy("canonical_record_v1")

    assert literal(record_denied, prev) == literal(record_executed, prev)
    assert canonical(record_denied, prev) != canonical(record_executed, prev)


# --------------------------------------------------------------------------
# Now buildable -- `orchestrator` (step 7) and `cli` (step 9) both exist.
# ---------------------------------------------------------------------------

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


def _good_plan(task_id: str) -> PlanModel:
    return PlanModel(
        plan_id="P000AUDITTEST",
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
    code = request.arguments["code"]
    assert "search_engine" not in code
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})
    return {"stdout": buf.getvalue(), "stderr": "", "exit_code": 0}


def _fake_rag_search(request: Any) -> dict[str, Any]:
    """A fake `rag.search` backend that still emits `EVIDENCE_RETRIEVED` --
    the one section 6.12 event type the real `app.rag.backend.rag_search_backend`
    emits and no fake elsewhere in this suite bothers to (they do not need
    it; this test's whole point is that every type is emitted somewhere)."""
    results = list(_DEFAULT_EVIDENCE)
    append_event(
        request.capability.task_id,
        request.capability.agent_id,
        EventType.EVIDENCE_RETRIEVED,
        {"query": request.arguments.get("query"), "returned_count": len(results)},
    )
    return {"results": results}


def _register_fake_backends() -> None:
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


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(autouse=True)
def _clean_gateway_state():
    clear_backends()
    yield
    clear_backends()


def test_every_event_type_emitted_during_the_happy_path_run(db, monkeypatch):
    """Section 9 Audit: every type in section 6.12's list emitted at least
    once during one happy-path run.

    A single straight-through task never touches `TOOL_DENIED` or
    `APPROVAL_REJECTED` on its own -- those are section 1.2/5.3's own
    scenarios. One coherent run is built here exactly the way section 1's
    three walkthroughs combine in the actual demo: submit, reject once
    (section 5.3's one bounded revision -> `APPROVAL_REJECTED`), approve
    (`APPROVAL_GRANTED` / `ARTIFACT_RELEASED`), plus one direct Tool Gateway
    denial (`TOOL_DENIED`) against the same task/agent -- the CLI's own
    `/trace` (section 1.1's closing line) is what a human reads all of this
    through.
    """
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

    _register_fake_backends()
    _stub_generate_plan(monkeypatch)
    client = TestClient(create_app())  # no `with` -- avoids the Ollama-dependent startup hook
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    approver_headers = _login(client, "a.singh", "approver-pw")

    submit = client.post(
        "/task",
        json={
            "text": "Identify Pump P-101's recent maintenance history and summarize it.",
            "classification": Classification.CONFIDENTIAL,
        },
        headers=engineer_headers,
    )
    assert submit.status_code == 201, submit.text
    body = submit.json()
    assert body["status"] == TaskStatus.WAITING_FOR_APPROVAL, body
    task_id, agent_id = body["task_id"], None

    trace = client.get(f"/tasks/{task_id}/trace", headers=engineer_headers).json()
    agent_id = next(e["actor_id"] for e in trace["events"] if e["event_type"] == "AGENT_STARTED")
    approval_id = next(
        e["payload"]["approval_id"] for e in trace["events"] if e["event_type"] == "APPROVAL_REQUESTED"
    )

    # section 5.3's one bounded revision -> APPROVAL_REJECTED, then a fresh
    # ARTIFACT_CREATED/ARTIFACT_VERIFIED/APPROVAL_REQUESTED cycle.
    reject = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "REJECTED", "comment": "Add more detail."},
        headers=approver_headers,
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["task_status"] == TaskStatus.WAITING_FOR_APPROVAL, reject.json()

    trace = client.get(f"/tasks/{task_id}/trace", headers=engineer_headers).json()
    approval_id = next(
        e["payload"]["approval_id"]
        for e in reversed(trace["events"])
        if e["event_type"] == "APPROVAL_REQUESTED"
    )

    approve = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "APPROVED", "comment": "Looks correct now."},
        headers=approver_headers,
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["task_status"] == TaskStatus.COMPLETED, approve.json()

    # One direct denial on the same task/agent (section 1.2's own shape): a
    # still-valid capability, a resource whose ACL is disjoint from the
    # task's department.
    from app.capability import issue_for_step
    from app.policy.context import PolicyResource
    from app.tool_gateway import invoke

    cap = issue_for_step(task_id, agent_id, Tool.RAG_SEARCH)
    denial = invoke(
        capability_token=cap.token,
        tool=Tool.RAG_SEARCH,
        resource=PolicyResource.build(
            resource_id="DOC-FIN-Q3",
            type="document",
            classification=Classification.CONFIDENTIAL,
            acl=["finance"],
        ),
        arguments={"query": "Q3 finance report"},
    )
    assert denial["success"] is False
    assert denial["error"]["code"] == "POLICY_DENIED"

    events = get_trace(None)
    seen = {e.event_type for e in events}
    missing = set(ALL_EVENT_TYPES) - seen
    assert not missing, f"section 6.12 types never emitted in this run: {sorted(missing)}"


def test_trace_shows_denial_and_emergency_control_events(db):
    """Section 9 Audit: `/trace` shows `TOOL_DENIED` and the emergency-
    control event, not only the happy path -- and, per section 1.2's own
    point, as one more ordinary line, not an error dump. This is exactly
    what `citadel trace <task_id>` (`cli/main.py`) prints verbatim via this
    endpoint's own `text` field.
    """
    engineer = create_user(
        db,
        username="j.rao",
        password="engineer-pw",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
        user_id="U123",
    )
    admin = create_user(
        db,
        username="s.mehta",
        password="admin-pw",
        roles=[Role.ADMIN],
        clearance=Classification.CONFIDENTIAL,
        department="security",
        user_id="U-ADMIN",
    )
    db.commit()

    from app.db.models import Agent, Task

    task = Task(
        task_id="T-AUDIT-TRACE",
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True},
    )
    db.add(task)
    db.flush()
    db.add(Agent(agent_id="A-AUDIT-TRACE", task_id=task.task_id, agent_type="researcher"))
    db.commit()

    client = TestClient(create_app())
    engineer_headers = _login(client, "j.rao", "engineer-pw")
    admin_headers = _login(client, "s.mehta", "admin-pw")

    # A denial-path event: a still-valid capability, an ACL-disjoint resource.
    from app.capability import issue_for_step
    from app.policy.context import PolicyResource
    from app.tool_gateway import invoke

    cap = issue_for_step(task.task_id, "A-AUDIT-TRACE", Tool.RAG_SEARCH)
    invoke(
        capability_token=cap.token,
        tool=Tool.RAG_SEARCH,
        resource=PolicyResource.build(
            resource_id="DOC-FIN-Q3", type="document", classification=Classification.CONFIDENTIAL, acl=["finance"]
        ),
        arguments={"query": "Q3 finance report"},
    )

    # The emergency-control event (section 1.3): admin-only, disables a tool
    # centrally, independent of any already-issued capability.
    disable = client.post(
        f"/admin/tools/{Tool.PYTHON_EXECUTE}/disable", json={"disabled": True}, headers=admin_headers
    )
    assert disable.status_code == 200, disable.text

    # A second, still-valid capability now denied purely by the emergency
    # control (section 1.3's own proof), recorded on the same task.
    cap2 = issue_for_step(task.task_id, "A-AUDIT-TRACE", Tool.PYTHON_EXECUTE)
    tool_disabled_denial = invoke(
        capability_token=cap2.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=PolicyResource.build(
            resource_id=task.task_id, type="task_scope", classification=Classification.CONFIDENTIAL,
            acl=["maintenance"],
        ),
        arguments={"code": "print(1)"},
    )
    assert tool_disabled_denial["error"]["code"] == "TOOL_DISABLED"

    response = client.get(f"/tasks/{task.task_id}/trace", headers=engineer_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    event_types = [e["event_type"] for e in body["events"]]
    assert event_types.count(EventType.TOOL_DENIED) == 2

    # The exact rendering `citadel trace` prints verbatim (`cli/main.py`).
    text = body["text"]
    assert text == format_trace(get_trace(task.task_id))
    denied_lines = [line for line in text.splitlines() if "TOOL_DENIED" in line]
    assert len(denied_lines) == 2
    # Every line in the trace -- happy-path or denial -- is rendered by the
    # exact same formatter, in the exact same shape: no separate "error"
    # styling exists for a denial (section 1.2's own point).
    for line in text.splitlines():
        assert line.split()[0].startswith("EVT")
