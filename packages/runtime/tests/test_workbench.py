"""The workbench runtime end to end: a task the planner splits across helper agents, a
person pausing and steering the work, questions typed at the command line (/ask), and
the memory manager grounding the next plan in what the last one established.

Same stack as test_agent.py -- real queue, journal, chokepoint, tools, templates,
Postgres and pgvector, and a scripted model behind a real HTTP Ollama API that only sees
the prompts the runtime builds. Hooks on the scripted model let a test act at a precise
moment of a run (a person pressing pause while a helper agent is mid-step).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from citadel_contracts.domain import User
from citadel_deliverables import read_bytes, rendered_text
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.keyring import init_keys, load_receipt_public_key, load_receipt_signing_key
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_runtime import (
    Journal,
    Runtime,
    Worker,
    get_task,
    request_cancel,
    request_pause,
    request_resume,
    submit,
)
from citadel_tools import Chokepoint, DataBoundary, LocalSandboxRunner
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector
from scripted_brain import LATHE_REFERENCE, citadel_brain, recording, with_hooks
from stack_fixtures import REPO_ROOT, enabled_registry, gateway_for, ingest_corpus, start_fake

ENGINEER_1 = User("demo-engineer-1", "R. Kulkarni", ("engineer",), "internal", "process-engineering")
APPROVER = User("demo-approver", "A. Menon", ("approver",), "confidential", "quality-assurance")
USERS = {u.user_id: u for u in (ENGINEER_1, APPROVER)}

LATHE_GOAL = ("Report on lathe L-1 in machine shop sector 1: its condition and workload, what company policy "
              "requires today, the options, and a recommendation.")
REFERENCE_TITLES = {"Technology digest - CNC turning centres and conventional lathes",
                    "Vendor catalogue extract - 2-axis CNC turning centres"}

pytestmark = [requires_pgvector, pytest.mark.integration]

Hook = Callable[[str, str], None]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    from citadel_knowledge.ocr import available

    if not available():
        pytest.skip("tesseract is not installed on this machine")
    registry = enabled_registry()
    seen: list[tuple[str, str]] = []
    hooks: list[Hook] = []
    fake = start_fake(registry, with_hooks(recording(citadel_brain, seen), hooks))
    try:
        with pg_scratch_db() as env:
            apply_all_migrations(env)
            data_dir = DataDir(root=tmp_path_factory.mktemp("data"))
            audit = AuditLog(env, registry.event_registry())
            db = ingest_corpus(env, data_dir, gateway_for(registry, fake, audit=None))
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
            runtime.gateway.warm_resident_set()
            yield SimpleNamespace(env=env, db=db, rt=runtime, seen=seen, hooks=hooks, registry=registry,
                                  data_dir=data_dir, audit=audit)
    finally:
        fake.stop()


def _submit(world: SimpleNamespace, goal: str, *, kind: str = "task", user: User = ENGINEER_1) -> dict[str, Any]:
    return submit(world.db, user=user, goal=goal, classification=None, profile_ceiling="CONFIDENTIAL",
                  audit=world.audit, kind=kind)


def _drain(world: SimpleNamespace) -> None:
    worker = Worker(world.rt, "test-worker")
    while worker.run_once() is not None:
        pass


def _journal(world: SimpleNamespace, task_id: str) -> list[dict[str, Any]]:
    return Journal(world.db, task_id).entries(limit=2000)


def _agents(world: SimpleNamespace, task_id: str) -> list[dict[str, Any]]:
    db: Database = world.db
    return db.query(
        "SELECT agent_id, name, role, status, depends_on, findings, evidence, current_step FROM task_agents "
        "WHERE task_id = %(t)s::uuid ORDER BY agent_id",
        {"t": task_id},
    )


def _shared(world: SimpleNamespace, task_id: str) -> list[dict[str, Any]]:
    db: Database = world.db
    return db.query(
        "SELECT kind, content, evidence, author, agent_id, addressed_to FROM task_shared_state "
        "WHERE task_id = %(t)s::uuid ORDER BY id",
        {"t": task_id},
    )


def _event_seq(entries: list[dict[str, Any]], agent: str, event: str) -> int:
    return int(next(e["step_seq"] for e in entries
                if e["agent_id"] == agent and e["step_type"] == "agent" and e["payload"].get("event") == event))


def test_the_planner_splits_the_lathe_report_across_agents_and_the_lead_writes_it(world: SimpleNamespace):
    task = _submit(world, LATHE_GOAL)
    _drain(world)
    task = get_task(world.db, task["id"]) or {}
    entries = _journal(world, task["id"])
    assert task["status"] == "completed", (task["error"], entries[-4:])

    agents = _agents(world, task["id"])
    assert [a["agent_id"] for a in agents] == ["agent_1", "agent_2", "agent_3", "lead"]
    assert all(a["status"] == "done" for a in agents), agents
    assert [a["name"] for a in agents[:3]] == ["Agent 1", "Agent 2", "Agent 3"]
    assert agents[2]["depends_on"] == ["agent_1"] and all(a["evidence"] for a in agents[:3])

    # Agents 1 and 2 worked side by side; Agent 3 waited for Agent 1's findings.
    assert _event_seq(entries, "agent_3", "started") > _event_seq(entries, "agent_1", "done")
    assert _event_seq(entries, "agent_2", "started") < _event_seq(entries, "agent_1", "done")

    # Each tool call is tagged with the agent that made it; the reference library is only
    # ever reached by web.search, and docs.search never returns it.
    results = [e for e in entries if e["step_type"] == "tool_result"]
    by_agent = {(e["agent_id"], e["payload"]["tool"]) for e in results}
    assert ("agent_2", "web.search") in by_agent and ("agent_3", "docs.diff") in by_agent
    web = [e["payload"] for e in results if e["payload"]["tool"] == "web.search"]
    assert {h["title"] for h in web[0]["detail"]["search"]["hits"]} <= REFERENCE_TITLES
    searched = [h["title"] for e in results if e["payload"]["tool"] == "docs.search"
                for h in e["payload"]["detail"]["search"]["hits"]]
    assert searched and not set(searched) & REFERENCE_TITLES
    # The engineer is internal: the confidential quotation comparison is withheld, and says so.
    denied = [d for e in results if e["payload"]["tool"] == "docs.search" for d in e["payload"]["detail"]["search"]["denied"]]
    assert any(d["reason"] == "classification_exceeds_actor_max" for d in denied)

    # The shared state: the planner's assumption, a helper's fact, and the policy conflict
    # Agent 3 found -- a question for a person, citing the evidence it rests on.
    shared = _shared(world, task["id"])
    kinds = [s["kind"] for s in shared]
    assert "assumption" in kinds and "plan" in kinds and "fact" in kinds
    question = next(s for s in shared if s["kind"] == "question")
    assert question["content"].startswith("Policy conflict") and question["evidence"] and question["agent_id"] == "agent_3"

    # The lead wrote the report from everyone's findings -- one section per thing the
    # person asked for, in their order -- and it is the person's to keep or change.
    artifact, data = read_bytes(world.db, world.data_dir, _report_id(task))
    assert artifact["status"] == "VERIFIED" and artifact["template_id"] == "report" and not artifact["requires_approval"]
    body = artifact["provenance"]["render_inputs"]["content"]["body"]
    assert [s["heading"] for s in body] == ["Condition and workload", "What company policy requires today", "Options"]
    text = rendered_text("docx", data)
    assert "31.2" in text and "What company policy requires today" in text and "Recommendations" in text
    assert artifact["verification"]["passed"] and not artifact["verification"]["flagged_claims"]
    assert task["result"]["agents"] == ["agent_1", "agent_2", "agent_3"]


def _report_id(task: dict[str, Any]) -> str:
    reports = [a for a in task["result"]["artifacts"] if a.get("template_id") == "report"]
    return str(reports[-1]["id"])


def test_a_report_follows_the_request_and_one_agent_can_write_it_alone(world: SimpleNamespace):
    task = _submit(world, "Report on lathe L-1 covering only its condition and the vendor options")
    _drain(world)
    task = get_task(world.db, task["id"]) or {}
    assert task["status"] == "completed", task["error"]
    assert _agents(world, task["id"]) == []  # nothing here needed splitting
    tools = [e["payload"]["tool"] for e in _journal(world, task["id"]) if e["step_type"] == "tool_result"]
    assert tools == ["docs.search", "web.search", "doc.generate"]
    artifact, data = read_bytes(world.db, world.data_dir, _report_id(task))
    content = artifact["provenance"]["render_inputs"]["content"]
    assert [s["heading"] for s in content["body"]] == ["Condition", "Vendor options"]
    assert "recommendations" not in content  # nobody asked for one
    text = rendered_text("docx", data)
    assert "Spindle runout is 0.045 mm" in text and "KT-2A" in text and "Policy 1" not in text


def test_the_asker_edits_the_report_and_the_agents_revise_from_that_edit(world: SimpleNamespace):
    from citadel_deliverables import TaskFacts, cited_ids, generate
    from citadel_knowledge import evidence as resolve
    from citadel_runtime import after_edit, request_revision

    task = _submit(world, "Report on lathe L-1 covering only its condition and the vendor options")
    _drain(world)
    task = get_task(world.db, task["id"]) or {}
    first, _ = read_bytes(world.db, world.data_dir, _report_id(task))
    template = next(t for t in world.registry.templates if t.id == "report")

    def edit(content: dict[str, Any], note: str) -> Any:
        generated = generate(world.db, world.data_dir, REPO_ROOT / "registry", template, content,
                             task=TaskFacts(task["id"], "INTERNAL", str(task["goal"])), author=ENGINEER_1,
                             evidence=resolve(world.db, task["id"], cited_ids(template, content)),
                             revision_note=f"Edited by {ENGINEER_1.username}: {note}")
        after_edit(world.db, task_id=task["id"], editor=ENGINEER_1, artifact_id=generated.artifact_id,
                   version=generated.version, status=generated.status, requires_approval=False, note=note)
        return generated

    # The person retitles it by hand: a new version, re-verified, journalled as theirs.
    mine = dict(first["provenance"]["render_inputs"]["content"])
    mine["title"] = "Lathe L-1 - the case for replacement"
    edited = edit(mine, "retitled")
    assert edited.status == "VERIFIED" and edited.version == 2
    # A citation the task was never given fails verification and changes nothing else.
    bad = edit({**mine, "summary": "Invented [E99]."}, "a bad edit")
    assert bad.status == "TEMP" and (get_task(world.db, task["id"]) or {})["status"] == "completed"
    human = [e for e in _journal(world, task["id"]) if e["step_type"] == "human"]
    assert [h["payload"]["action"] for h in human] == ["edited", "edited"] and human[0]["agent_id"] == "human:demo-engineer-1"

    # Then asks the agents for more. They start from the person's version, not their own.
    request_revision(world.db, task["id"], user=ENGINEER_1, instruction="Add a section on operator training")
    assert Worker(world.rt, "test-worker").run_once() == "completed"
    task = get_task(world.db, task["id"]) or {}
    latest, data = read_bytes(world.db, world.data_dir, _report_id(task))
    content = latest["provenance"]["render_inputs"]["content"]
    assert content["title"] == "Lathe L-1 - the case for replacement"  # the hand edit survived
    assert [s["heading"] for s in content["body"]] == ["Condition", "Vendor options", "Operator training"]
    assert latest["version"] == 4 and latest["status"] == "VERIFIED"
    assert latest["provenance"]["render_inputs"]["revision_note"].startswith("Revised at R. Kulkarni's request")
    assert "3 to 4 weeks of training" in rendered_text("docx", data)
    # ...from the person's last VERIFIED version (v2), not the broken draft after it (v3).
    revision = next(e for e in _journal(world, task["id"]) if e["step_type"] == "revision")
    assert revision["payload"]["kind"] == "owner" and revision["payload"]["from_version"] == 2


def test_a_person_pauses_the_team_steers_the_lead_and_the_work_resumes(world: SimpleNamespace):
    task = _submit(world, LATHE_GOAL)
    pressed: list[bool] = []

    def pause_while_agent_2_works(system: str, user: str) -> None:
        if not pressed and "YOUR PART:" in user and LATHE_REFERENCE in user:
            pressed.append(True)
            request_pause(world.db, task["id"], user=ENGINEER_1, audit=world.audit)

    world.hooks.append(pause_while_agent_2_works)
    try:
        status = Worker(world.rt, "test-worker").run_once()
    finally:
        world.hooks.remove(pause_while_agent_2_works)
    assert status == "paused"
    paused = get_task(world.db, task["id"]) or {}
    assert paused["status"] == "paused" and paused["pause_requested"]
    assert {a["status"] for a in _agents(world, task["id"]) if a["agent_id"] != "lead"} <= {"paused", "done"}
    assert Worker(world.rt, "test-worker").run_once() is None  # a paused task is not claimed

    # While it is paused, the person tells the lead something.
    world.db.execute(
        "INSERT INTO task_shared_state (task_id, kind, content, author, addressed_to) VALUES "
        "(%(t)s::uuid, 'note', 'Tell the approver the KT-2A swing is too small for our sleeves.', "
        "'human:demo-engineer-1', 'lead')",
        {"t": task["id"]},
    )
    request_resume(world.db, task["id"], user=ENGINEER_1, audit=world.audit)
    assert Worker(world.rt, "test-worker").run_once() == "completed"

    entries = _journal(world, task["id"])
    kinds = [e["step_type"] for e in entries]
    assert "paused" in kinds and kinds.count("resumed") >= 1
    assert kinds.count("planned") == 1  # resumed, not re-planned
    web_calls = [e for e in entries if e["step_type"] == "tool_call" and e["payload"]["tool"] == "web.search"]
    assert len(web_calls) == 1  # Agent 2 carried on from its journal; it did not repeat its search
    assert any(e["step_type"] == "agent" and e["payload"].get("event") == "resumed" for e in entries)
    steered = [e for e in entries if e["step_type"] == "steered"]
    assert len(steered) == 1 and steered[0]["agent_id"] == "lead"  # told once, and journalled

    task = get_task(world.db, task["id"]) or {}
    _, data = read_bytes(world.db, world.data_dir, _report_id(task))
    assert "KT-2A swing is too small" in rendered_text("docx", data)


def test_ask_what_changed_in_a_policy_is_answered_from_a_diff(world: SimpleNamespace):
    task = _submit(world, "the difference in policy 1 between today and last month", kind="ask")
    _drain(world)
    task = get_task(world.db, task["id"]) or {}
    assert task["status"] == "completed", task["error"]
    answer = task["result"]["answer"]
    assert "40%" in answer and "30%" in answer and "2026-08-01" in answer and "2026-09-01" in answer
    assert len(task["result"]["citations"]) >= 2

    entries = _journal(world, task["id"])
    purposes = [e["payload"]["purpose"] for e in entries if e["step_type"] == "model_call"]
    assert purposes and set(purposes) == {"act"}  # a question is answered, not planned
    tools = [e["payload"]["tool"] for e in entries if e["step_type"] == "tool_result"]
    assert tools == ["docs.search", "docs.diff"]
    offered = next(e["payload"]["tools"] for e in entries if e["step_type"] == "claimed")
    assert "doc.generate" not in offered and "state.note" not in offered and "code.run" not in offered
    assert "workbench.inspect" in offered


def test_ask_about_work_in_progress_reads_the_workbench_itself(world: SimpleNamespace):
    waiting = _submit(world, LATHE_GOAL)  # queued, not yet run: questions are claimed first
    question = _submit(world, "what is the lathe report waiting for?", kind="ask")
    assert Worker(world.rt, "test-worker").run_once() == "completed"
    answered = get_task(world.db, question["id"]) or {}
    assert answered["status"] == "completed", answered["error"]
    assert answered["result"]["answer"].startswith("Where the work stands")
    entries = _journal(world, question["id"])
    assert [e["payload"]["tool"] for e in entries if e["step_type"] == "tool_result"] == ["workbench.inspect"]
    assert (get_task(world.db, waiting["id"]) or {})["status"] == "submitted"
    request_cancel(world.db, waiting["id"], user=ENGINEER_1)


def test_what_a_task_established_is_remembered_and_grounds_the_next_plan(world: SimpleNamespace):
    planner_prompts: list[str] = []

    def capture(system: str, user: str) -> None:
        if system.startswith("You are the planner"):
            planner_prompts.append(system)

    world.hooks.append(capture)
    try:
        first = _submit(world, LATHE_GOAL)
        _drain(world)
        second = _submit(world, LATHE_GOAL)
        _drain(world)
    finally:
        world.hooks.remove(capture)

    memories = world.db.query(
        "SELECT tier, memory_type, subject, content, classification, acl, status, source FROM memories "
        "WHERE status = 'active' ORDER BY created_at"
    )
    lathe = [m for m in memories if m["subject"] == "Lathe L-1"]
    events = world.db.query("SELECT operation, candidate, reason, task_id::text AS task_id FROM memory_events ORDER BY id")
    assert len(lathe) == 1, (lathe, events)  # the same fact twice is one memory, updated -- not two
    assert lathe[0]["tier"] == "semantic" and lathe[0]["classification"] == "internal"
    assert lathe[0]["acl"] == ["process-engineering"]
    assert any(m["tier"] == "episodic" and m["memory_type"] == "outcome" for m in memories)
    operations = {r["operation"] for r in world.db.query(
        "SELECT operation FROM memory_events WHERE task_id = %(t)s::uuid", {"t": second["id"]})}
    assert "update" in operations or "ignore" in operations

    # The second plan was made knowing what the first established.
    assert planner_prompts and "What the workbench remembers" in planner_prompts[-1]
    assert "Lathe L-1" in planner_prompts[-1]
    recalled = next(e for e in _journal(world, second["id"]) if e["step_type"] == "recalled")
    assert recalled["payload"]["count"] >= 1
    assert any(e["step_type"] == "memory" for e in _journal(world, first["id"]))
    # ...and a different department never recalls it.
    from citadel_memory import MemoryManager, MemoryScope

    other = MemoryManager(world.db).recall("lathe L-1 repair cost", MemoryScope("instrumentation", "CONFIDENTIAL", "x"))
    assert not [m for m in other if m.subject == "Lathe L-1"]
