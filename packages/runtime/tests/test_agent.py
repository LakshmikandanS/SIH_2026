"""The runtime end to end: real queue, real journal, real chokepoint, real tools, real
templates, real Postgres and pgvector, real Tesseract, the real process sandbox -- and a
scripted model behind a real HTTP Ollama API that only ever sees the prompts the
runtime builds. One test per acceptance target, plus the loop's own guarantees
(revision, cancellation, budgets, resumption).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from citadel_contracts.domain import User
from citadel_deliverables import decide, read_bytes, rendered_text
from citadel_platform.audit.log import AuditLog
from citadel_platform.keyring import init_keys, load_receipt_public_key, load_receipt_signing_key
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_runtime import (
    BudgetLimits,
    Journal,
    Runtime,
    TaskError,
    Worker,
    after_decision,
    get_task,
    request_cancel,
    submit,
)
from citadel_tools import Chokepoint, DataBoundary, LocalSandboxRunner
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector
from scripted_brain import citadel_brain, recording
from stack_fixtures import REPO_ROOT, enabled_registry, gateway_for, ingest_corpus, start_fake

ENGINEER_1 = User("demo-engineer-1", "R. Kulkarni", ("engineer",), "internal", "process-engineering")
ENGINEER_2 = User("demo-engineer-2", "S. Nair", ("engineer",), "confidential", "instrumentation")
APPROVER = User("demo-approver", "A. Menon", ("approver",), "confidential", "quality-assurance")
USERS = {u.user_id: u for u in (ENGINEER_1, ENGINEER_2, APPROVER)}

pytestmark = [requires_pgvector, pytest.mark.integration]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    from citadel_knowledge.ocr import available

    if not available():
        pytest.skip("tesseract is not installed on this machine")
    registry = enabled_registry()
    seen: list[tuple[str, str]] = []
    fake = start_fake(registry, recording(citadel_brain, seen))
    try:
        with pg_scratch_db() as env:
            apply_all_migrations(env)
            data_dir = DataDir(root=tmp_path_factory.mktemp("data"))
            audit = AuditLog(env, registry.event_registry())
            gateway = gateway_for(registry, fake, audit=None)
            db = ingest_corpus(env, data_dir, gateway)
            keys = tmp_path_factory.mktemp("keys")
            init_keys(keys)
            key_env = {"CITADEL_KEYS_DIR": str(keys)}
            public = load_receipt_public_key(key_env)
            runtime = Runtime(
                db=db,
                registry=registry,
                registry_dir=REPO_ROOT / "registry",
                data_dir=data_dir,
                gateway=gateway_for(registry, fake, audit=audit, tracer=Tracer(db)),
                chokepoint=Chokepoint(registry, signing_key=load_receipt_signing_key(key_env)),
                boundary=DataBoundary(public),
                sandbox=LocalSandboxRunner(public),
                audit=audit,
                tracer=Tracer(db),
                lookup_user=USERS.get,
            )
            # What the worker does at startup: pin the profile's resident set, so routing
            # scores residency the way the demonstration box will.
            runtime.gateway.warm_resident_set()
            yield SimpleNamespace(env=env, db=db, rt=runtime, seen=seen, registry=registry, data_dir=data_dir, audit=audit)
    finally:
        fake.stop()


def _run(world: SimpleNamespace, user: User, goal: str, classification: str | None = None, **limits: Any) -> dict[str, Any]:
    task = submit(world.db, user=user, goal=goal, classification=classification, profile_ceiling="CONFIDENTIAL",
                  audit=world.audit)
    rt = world.rt if not limits else dataclasses.replace(world.rt, limits=BudgetLimits(**limits))
    worker = Worker(rt, "test-worker")
    while worker.run_once() is not None:
        pass
    refreshed = get_task(world.db, task["id"])
    assert refreshed is not None
    return refreshed


def _journal(world: SimpleNamespace, task_id: str) -> list[dict[str, Any]]:
    return Journal(world.db, task_id).entries(limit=1000)


def test_submission_refuses_a_classification_above_clearance(world: SimpleNamespace):
    with pytest.raises(TaskError, match="above your clearance"):
        submit(world.db, user=ENGINEER_1, goal="x", classification="CONFIDENTIAL", profile_ceiling="CONFIDENTIAL")
    with pytest.raises(TaskError, match="needs a goal"):
        submit(world.db, user=ENGINEER_1, goal="   ", classification=None, profile_ceiling="CONFIDENTIAL")


def test_target_b_scanned_report_to_a_verified_approval_note_then_release(world: SimpleNamespace):
    goal = "Prepare an approval note for continued service of heat exchanger E-101 based on the latest inspection."
    task = _run(world, ENGINEER_1, goal)
    assert task["status"] == "awaiting_approval", (task["error"], _journal(world, task["id"])[-3:])
    result = task["result"]
    assert result["awaiting_approval"] and result["citations"]
    kinds = [e["step_type"] for e in _journal(world, task["id"])]
    assert kinds[:3] == ["submitted", "claimed", "model_call"] and "planned" in kinds
    assert kinds.count("tool_result") >= 3 and kinds[-1] == "finished"
    tools = [e["payload"]["tool"] for e in _journal(world, task["id"]) if e["step_type"] == "tool_result"]
    assert tools[:3] == ["docs.search", "calc.evaluate", "doc.generate"]

    artifact_id = result["awaiting_approval"][0]
    artifact, data = read_bytes(world.db, world.data_dir, artifact_id)
    assert artifact["status"] == "VERIFIED" and artifact["classification"] == "internal"
    text = rendered_text("docx", data)
    assert "9.2 mm" in text and "Inspection report" in text and "PENDING APPROVAL" in text

    outcome = decide(world.db, world.data_dir, REPO_ROOT / "registry", list(world.registry.templates),
                     artifact_id=artifact_id, approver=APPROVER, approve=True, comment="Agreed.", audit=world.audit)
    task = after_decision(world.db, task_id=task["id"], approved=True, comment="Agreed.", approver=APPROVER, audit=world.audit)
    assert outcome["status"] == "RELEASED" and task["status"] == "completed"


def test_a_rejection_buys_exactly_one_revision(world: SimpleNamespace):
    goal = "Draft an approval note for heat exchanger E-101 from the inspection evidence."
    task = _run(world, ENGINEER_1, goal)
    first = task["result"]["awaiting_approval"][0]
    decide(world.db, world.data_dir, REPO_ROOT / "registry", list(world.registry.templates),
           artifact_id=first, approver=APPROVER, approve=False, comment="State the re-inspection method.")
    task = after_decision(world.db, task_id=task["id"], approved=False, comment="State the re-inspection method.",
                          approver=APPROVER)
    assert task["status"] == "revision_required" and task["revision_count"] == 1
    worker = Worker(world.rt, "test-worker")
    assert worker.run_once() == "awaiting_approval"
    revised = get_task(world.db, task["id"])
    assert revised is not None
    task = revised
    second = task["result"]["awaiting_approval"][0]
    assert second != first
    _, data = read_bytes(world.db, world.data_dir, second)
    text = rendered_text("docx", data)
    assert "ultrasonic thickness survey" in text and "rejected at review" in text
    decide(world.db, world.data_dir, REPO_ROOT / "registry", list(world.registry.templates),
           artifact_id=second, approver=APPROVER, approve=False, comment="Still not right.")
    task = after_decision(world.db, task_id=task["id"], approved=False, comment="Still not right.", approver=APPROVER)
    assert task["status"] == "failed" and "one revision" in (task["error"] or "")


def test_target_a_two_task_types_route_to_two_models_with_a_visible_reason(world: SimpleNamespace):
    world.seen.clear()
    code_task = _run(world, ENGINEER_1, "Write a Python script to find the CML with the least remaining life.")
    assert code_task["status"] == "completed", code_task["error"]
    calls = [e["payload"] for e in _journal(world, code_task["id"]) if e["step_type"] == "model_call"]
    plan_call = next(c for c in calls if c["purpose"] == "plan")
    act_calls = [c for c in calls if c["purpose"] == "act"]
    code_model = {m.id: m.tag for m in world.registry.models}
    assert act_calls and all(c["model_id"] != plan_call["model_id"] for c in act_calls)
    assert all("code_generation" in c["reason"] or "task fit" in c["reason"] for c in act_calls)
    assert any(tag == code_model[act_calls[0]["model_id"]] for tag, _ in world.seen)
    # every candidate is listed with its score terms or the reason it was ineligible
    assert act_calls[0]["candidates"] and all(c["summary"] for c in act_calls[0]["candidates"])


def test_target_c_code_runs_in_the_sandbox_and_its_output_is_citable(world: SimpleNamespace):
    task = _run(world, ENGINEER_1, "Run a python script to compute remaining life from the CML readings.")
    assert task["status"] == "completed", task["error"]
    run = next(e["payload"] for e in _journal(world, task["id"])
               if e["step_type"] == "tool_result" and e["payload"]["tool"] == "code.run")
    assert run["status"] == "ok" and run["output"]["exit_code"] == 0
    assert "CML-3 remaining life 3.2 years" in run["output"]["stdout"]
    assert run["receipt"]["verified_by"] == "sandbox (process)"
    assert task["result"]["citations"] == ["C1"]


def test_target_d_a_stamp_is_re_read_with_the_vision_model(world: SimpleNamespace):
    task = _run(world, ENGINEER_1, "What does the stamp on the IR-2026-0147 inspection report say?")
    assert task["status"] == "completed", task["error"]
    vision = next(e["payload"] for e in _journal(world, task["id"])
                  if e["step_type"] == "tool_result" and e["payload"]["tool"] == "vision.extract")
    assert vision["status"] == "ok" and "QA INSPECTED" in vision["output"]["text"]
    assert "QA INSPECTED 14 AUG 2026" in task["result"]["answer"]
    model_calls = [e["payload"] for e in _journal(world, task["id"]) if e["step_type"] == "model_call"]
    assert any(c["purpose"] == "vision.extract" for c in
               world.db.query("SELECT attributes ->> 'purpose' AS purpose FROM trace_spans WHERE task_id = %(t)s::uuid "
                              "AND kind = 'model'", {"t": task["id"]}))
    assert model_calls  # the planning and acting calls are journalled beside it


def test_the_same_question_yields_different_citations_for_different_people(world: SimpleNamespace):
    question = "Summarise the E-101 flange leak and its corrosion history."
    one = _run(world, ENGINEER_1, question)
    two = _run(world, ENGINEER_2, question, "CONFIDENTIAL")
    searches = []
    for task in (one, two):
        result = next(e["payload"] for e in _journal(world, task["id"])
                      if e["step_type"] == "tool_result" and e["payload"]["tool"] == "docs.search")
        searches.append(result["detail"]["search"])
    titles = [{h["title"] for h in s["hits"]} for s in searches]
    reasons = [{d["reason"] for d in s["denied"]} for s in searches]
    assert titles[0] != titles[1]
    assert reasons[0] and reasons[1]


def test_cancellation_takes_effect_and_is_recorded(world: SimpleNamespace):
    task = submit(world.db, user=ENGINEER_1, goal="Summarise anything.", classification=None,
                  profile_ceiling="CONFIDENTIAL", audit=world.audit)
    cancelled = request_cancel(world.db, task["id"], user=ENGINEER_1, audit=world.audit)
    assert cancelled["status"] == "cancelled"
    assert Worker(world.rt, "test-worker").run_once() is None  # nothing left to claim


def test_the_step_budget_is_checked_every_iteration(world: SimpleNamespace):
    task = _run(world, ENGINEER_1, "Please loop forever adding numbers.", max_steps=3)
    assert task["status"] == "failed" and "steps budget exceeded" in (task["error"] or "")
    assert task["usage"]["steps"] == 3


def test_a_task_abandoned_mid_run_is_resumed_from_its_journal(world: SimpleNamespace):
    task = submit(world.db, user=ENGINEER_1, goal="Summarise the P-310 pump maintenance history.",
                  classification=None, profile_ceiling="CONFIDENTIAL")
    # a worker claimed it, searched once, and died
    # (the updated_at trigger would stamp now(); a dead worker's last heartbeat is an hour old)
    world.db.script([
        ("ALTER TABLE tasks DISABLE TRIGGER tasks_set_updated_at", None),
        ("UPDATE tasks SET status = 'running', worker_id = 'dead', updated_at = now() - interval '1 hour' "
         "WHERE id = %(t)s::uuid", {"t": task["id"]}),
        ("ALTER TABLE tasks ENABLE TRIGGER tasks_set_updated_at", None),
    ])
    journal = Journal(world.db, task["id"])
    journal.append("planned", {"plan": {"steps": ["Search", "Answer"], "primary_capability": "reasoning", "deliverable": None}})
    journal.append("tool_call", {"step": 1, "tool": "docs.search", "arguments": {"query": "P-310"}})
    journal.append("tool_result", {"step": 1, "tool": "docs.search", "status": "ok", "summary": "1 passage",
                                   "for_model": {"status": "ok", "passages": [{"evidence_id": "E1", "text": "P-310 seal replaced"}]}})
    world.db.execute("INSERT INTO task_memory (task_id, key, value) VALUES (%(t)s::uuid, 'plan', %(p)s::jsonb)",
                     {"t": task["id"], "p": '{"steps": ["Search", "Answer"], "primary_capability": "reasoning", "deliverable": null}'})
    status = Worker(world.rt, "rescuer").run_once()
    assert status == "completed"
    entries = _journal(world, task["id"])
    claimed = next(e for e in entries if e["step_type"] == "claimed")
    assert claimed["payload"]["resumed"] is True
    # it did not plan again, and it did not repeat the search it had already done
    assert not any(e["step_type"] == "planned" and e["step_seq"] > claimed["step_seq"] for e in entries)
    assert not any(e["step_type"] == "tool_call" and e["step_seq"] > claimed["step_seq"] for e in entries)
