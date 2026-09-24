"""The workbench over HTTP: write a report from a prompt, edit it by hand, send it back
to the agents with an instruction, and the surfaces around that loop -- drafts and their
commit button, /ask, notes and a person's own tool runs on a task, pause and resume, the
activity feed, the tools panel, the environment and sandbox state, observability, what
the workbench remembers, and a document's versions and their diff.

Real scratch database, the ingested demo corpus, a real worker, and the scripted model
behind a real HTTP Ollama API (tests/fakes).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from starlette.testclient import TestClient

from citadel_api.app import create_app
from citadel_api.deps import load_app_state
from citadel_gateway import Gateway
from citadel_gateway.ollama import OllamaProvider
from citadel_platform.keyring import init_keys, load_receipt_public_key, load_receipt_signing_key
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_runtime import Runtime, Worker
from citadel_tools import Chokepoint, DataBoundary, LocalSandboxRunner
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector
from scripted_brain import citadel_brain
from stack_fixtures import REPO_ROOT, enabled_registry, ingest_corpus, start_fake

pytestmark = [requires_pgvector, pytest.mark.integration]

REPORT_PROMPT = "Report on lathe L-1 covering only its condition and the vendor options"


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    from citadel_knowledge.ocr import available

    if not available():
        pytest.skip("tesseract is not installed on this machine")
    registry = enabled_registry()
    fake = start_fake(registry, citadel_brain)
    try:
        with pg_scratch_db() as env:
            apply_all_migrations(env)
            keys = tmp_path_factory.mktemp("keys")
            init_keys(keys)
            data_root = tmp_path_factory.mktemp("data")
            env = {**env, "CITADEL_KEYS_DIR": str(keys), "CITADEL_DATA_DIR": str(data_root),
                   "CITADEL_INFERENCE_ENDPOINT": fake.url, "CITADEL_PROFILE": "demo-local"}
            base = load_app_state(env, install_sovereignty=False)
            gateway = Gateway(registry, OllamaProvider(fake.url), audit=base.audit, tracer=base.tracer)
            state = dataclasses.replace(base, registry=registry, gateway=gateway)
            ingest_corpus(env, DataDir(root=data_root), gateway)
            gateway.warm_resident_set()
            public = load_receipt_public_key(env)
            runtime = Runtime(
                db=state.db, registry=registry, registry_dir=REPO_ROOT / "registry", data_dir=state.data_dir,
                gateway=gateway, chokepoint=Chokepoint(registry, signing_key=load_receipt_signing_key(env)),
                boundary=DataBoundary(public), sandbox=LocalSandboxRunner(public), audit=state.audit,
                tracer=Tracer(state.db),
            )
            with TestClient(create_app(state)) as client:
                yield SimpleNamespace(client=client, runtime=runtime, state=state)
    finally:
        fake.stop()


def _login(api: SimpleNamespace, user_id: str) -> dict[str, str]:
    reply = api.client.post("/api/auth/session", json={"user_id": user_id})
    assert reply.status_code == 200, reply.text
    return {"Authorization": f"Bearer {reply.json()['token']}"}


def _drain(api: SimpleNamespace) -> None:
    worker = Worker(api.runtime, "api-test")
    while worker.run_once() is not None:
        pass


def _report(api: SimpleNamespace, headers: dict[str, str], task_id: str) -> dict[str, Any]:
    detail = api.client.get(f"/api/tasks/{task_id}", headers=headers).json()
    reports = [a for a in detail["artifacts"] if a["template_id"] == "report"]
    content = api.client.get(f"/api/artifacts/{reports[-1]['id']}/content", headers=headers)
    assert content.status_code == 200, content.text
    return dict(content.json())


def test_a_report_is_written_from_a_prompt_edited_by_hand_and_revised_by_the_agents(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    created = api.client.post("/api/tasks", headers=engineer, json={"goal": REPORT_PROMPT, "title": "lathe report"})
    assert created.status_code == 201 and created.json()["title"] == "lathe report"
    task_id = created.json()["id"]
    _drain(api)

    opened = _report(api, engineer, task_id)
    assert opened["editable"] and opened["template"]["id"] == "report"
    assert [s["heading"] for s in opened["content"]["body"]] == ["Condition", "Vendor options"]
    assert opened["evidence"] and all(e["evidence_id"][0] in "EC" for e in opened["evidence"])
    v1 = opened["artifact"]["id"]

    # The person rewrites the summary and adds a section of their own, citing evidence
    # the task already holds.
    content = dict(opened["content"])
    held = opened["evidence"]
    cite = next((e for e in held if "0.045" in e["text"]),
                next(e for e in held if "condition report" in str(e["title"]).lower()))["evidence_id"]
    content["summary"] = f"L-1 no longer holds tolerance: spindle runout is 0.045 mm [{cite}]."
    content["body"] = [*content["body"], {"heading": "Shop view", "text": f"Runout of 0.045 mm is over the limit [{cite}]."}]
    saved = api.client.post(f"/api/artifacts/{v1}/edit", headers=engineer, json={"content": content, "note": "tightened"})
    assert saved.status_code == 201, saved.text
    generated = saved.json()["generated"]
    assert generated["status"] == "VERIFIED" and generated["version"] == 2
    assert saved.json()["task"]["status"] == "completed"
    # v1 is history now: only the newest version is edited, and only by its asker.
    assert not api.client.get(f"/api/artifacts/{v1}/content", headers=engineer).json()["editable"]
    assert api.client.post(f"/api/artifacts/{v1}/edit", headers=engineer, json={"content": content}).status_code == 409
    approver = _login(api, "demo-approver")
    refused = api.client.post(f"/api/artifacts/{generated['artifact_id']}/edit", headers=approver, json={"content": content})
    assert refused.status_code == 409 and "only the person who asked" in refused.json()["error"]
    # An edit citing evidence the task never held is saved as a draft that failed tier 3.
    bad = api.client.post(f"/api/artifacts/{generated['artifact_id']}/edit", headers=engineer,
                          json={"content": {**content, "summary": "Made up [E999]."}}).json()["generated"]
    assert bad["status"] == "TEMP"
    assert any(t["name"] == "citation" and t["status"] == "fail" for t in bad["verification"]["tiers"])

    # Then the agents are asked for more, and start from the person's verified version.
    revised = api.client.post(f"/api/tasks/{task_id}/revise", headers=engineer,
                              json={"instruction": "Add a section on operator training"})
    assert revised.status_code == 200 and revised.json()["status"] == "revision_required", revised.text
    assert api.client.post(f"/api/tasks/{task_id}/revise", headers=approver,
                           json={"instruction": "x y z"}).status_code == 409
    _drain(api)
    latest = _report(api, engineer, task_id)
    headings = [s["heading"] for s in latest["content"]["body"]]
    assert headings == ["Condition", "Vendor options", "Shop view", "Operator training"]
    assert latest["content"]["summary"].startswith("L-1 no longer holds tolerance")
    assert [v["version"] for v in latest["versions"]] == [1, 2, 3, 4]
    assert latest["versions"][1]["note"].startswith("Edited by R. Kulkarni")
    download = api.client.get(f"/api/artifacts/{latest['artifact']['id']}/download", headers=engineer)
    assert download.status_code == 200 and download.content[:2] == b"PK"

    feed = api.client.get(f"/api/activity?task_id={task_id}", headers=engineer).json()["items"]
    texts = " | ".join(i["text"] for i in feed)
    assert "R. Kulkarni edited the deliverable (v2, verified): tightened" in texts
    assert "R. Kulkarni asked for a revision of v2: Add a section on operator training" in texts
    assert "Lead agent is revising the deliverable from v2: Add a section on operator training" in texts
    # A deliverable in the feed is what it is and its sections -- never the draft dumped.
    assert "sections: Condition, Vendor options, Shop view, Operator training" in texts and "{'" not in texts

    # The workbench remembers how the task ended once, brought up to date by the revision.
    memories = api.client.get("/api/memory", headers=engineer).json()["memories"]
    outcomes = [m for m in memories if m["memory_type"] == "outcome" and "'lathe report'" in m["content"]]
    assert len(outcomes) == 1 and "(v4)" in outcomes[0]["content"], outcomes


def test_a_draft_is_written_in_the_editor_and_committed_as_a_task(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    draft = api.client.post("/api/drafts", headers=engineer, json={"name": "report on lathe"}).json()
    assert draft["body"] == "" and draft["name"] == "report on lathe"
    assert api.client.post(f"/api/drafts/{draft['id']}/commit", headers=engineer).status_code == 400  # empty
    updated = api.client.put(f"/api/drafts/{draft['id']}", headers=engineer, json={"body": REPORT_PROMPT})
    assert updated.status_code == 200 and updated.json()["body"] == REPORT_PROMPT
    other = _login(api, "demo-engineer-2")
    assert api.client.put(f"/api/drafts/{draft['id']}", headers=other, json={"body": "mine"}).status_code == 404
    committed = api.client.post(f"/api/drafts/{draft['id']}/commit", headers=engineer)
    assert committed.status_code == 201, committed.text
    task = committed.json()
    assert task["title"] == "report on lathe" and task["goal"] == REPORT_PROMPT and task["draft_id"] == draft["id"]
    assert api.client.delete(f"/api/drafts/{draft['id']}", headers=engineer).status_code == 404  # the record stays
    listed = api.client.get("/api/drafts", headers=engineer).json()["drafts"]
    assert any(d["id"] == draft["id"] and d["committed_task_id"] == task["id"] for d in listed)
    assert api.client.post(f"/api/tasks/{task['id']}/cancel", headers=engineer).json()["status"] == "cancelled"


def test_people_work_on_a_live_task_alongside_its_agents(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    task = api.client.post("/api/tasks", headers=engineer, json={"goal": REPORT_PROMPT}).json()
    # A person runs a tool on their own task: through the chokepoint, journalled as theirs.
    ran = api.client.post(f"/api/tasks/{task['id']}/tools/docs.search", headers=engineer,
                          json={"arguments": {"query": "lathe L-1 spindle runout"}})
    assert ran.status_code == 200 and ran.json()["status"] == "ok", ran.text
    held = ran.json()["evidence"]
    assert held
    approver = _login(api, "demo-approver")
    assert api.client.post(f"/api/tasks/{task['id']}/tools/docs.search", headers=approver,
                           json={"arguments": {"query": "x"}}).status_code == 403
    # Notes: a fact citing what the task holds; a steer to the lead; refusals that say why.
    fact = api.client.post(f"/api/tasks/{task['id']}/notes", headers=engineer,
                           json={"kind": "fact", "content": "Runout was re-measured last week.", "evidence": held[:1]})
    assert fact.status_code == 201, fact.text
    steer = api.client.post(f"/api/tasks/{task['id']}/notes", headers=engineer,
                            json={"kind": "note", "content": "Keep it to one page.", "to": "lead"})
    assert steer.status_code == 201 and steer.json()["to"] == "lead"
    assert api.client.post(f"/api/tasks/{task['id']}/notes", headers=engineer,
                           json={"kind": "fact", "content": "Unfounded.", "evidence": ["E77"]}).status_code == 400
    assert api.client.post(f"/api/tasks/{task['id']}/notes", headers=engineer,
                           json={"content": "hello there", "to": "agent_9"}).status_code == 400
    # Pause and resume a queued task.
    paused = api.client.post(f"/api/tasks/{task['id']}/pause", headers=engineer).json()
    assert paused["status"] == "paused"
    assert Worker(api.runtime, "api-test").run_once() is None
    assert api.client.post(f"/api/tasks/{task['id']}/resume", headers=engineer).json()["pause_requested"] is False
    _drain(api)
    detail = api.client.get(f"/api/tasks/{task['id']}", headers=engineer).json()
    assert detail["task"]["status"] == "completed"
    human = [e for e in detail["journal"] if str(e.get("agent_id") or "").startswith("human:")]
    assert {e["step_type"] for e in human} >= {"tool_call", "tool_result", "human"}
    assert any(e["step_type"] == "steered" for e in detail["journal"])  # the lead was told
    assert {s["kind"] for s in detail["shared_state"]} >= {"fact", "note"}

    state = api.client.get(f"/api/tasks/{task['id']}/state", headers=engineer).json()
    assert set(state) >= {"task", "shared_state", "agents", "artifacts", "resources", "history", "working_memory"}
    assert state["task"]["objective"] == REPORT_PROMPT and state["resources"]["mcp_servers"] == []
    assert state["artifacts"]["outputs"] and state["history"]["counts"]["human_steps"] >= 3
    assert state["agents"] and state["agents"][0]["name"] == "Lead agent"


def test_ask_answers_at_the_command_line(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    asked = api.client.post("/api/tasks", headers=engineer,
                            json={"goal": "the difference in policy 1 between today and last month", "kind": "ask"})
    assert asked.status_code == 201 and asked.json()["kind"] == "ask"
    _drain(api)
    detail = api.client.get(f"/api/tasks/{asked.json()['id']}", headers=engineer).json()
    assert detail["task"]["status"] == "completed"
    assert "30%" in detail["task"]["result"]["answer"] and "40%" in detail["task"]["result"]["answer"]
    assert api.client.post("/api/tasks", headers=engineer, json={"goal": "x", "kind": "gossip"}).status_code == 400


def test_the_panels_around_the_work(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    tools = api.client.get("/api/workbench/tools", headers=engineer).json()
    names = {t["name"]: t for t in tools["available"]}
    assert names["web.search"]["available"] and names["docs.search"]["available"]
    assert tools["history"] and tools["mcp_servers"] == [] and "running" in tools
    environment = api.client.get("/api/workbench/environment", headers=engineer).json()
    assert environment["tasks"]["by_status"] and "models" in environment["resources"]
    feed = api.client.get("/api/activity", headers=engineer).json()
    assert feed["items"] and feed["cursor"] >= feed["items"][-1]["id"]
    later = api.client.get(f"/api/activity?after={feed['cursor']}", headers=engineer).json()
    assert later["items"] == []
    observed = api.client.get("/api/observability", headers=engineer).json()
    for section in ("models", "agents", "database", "alerts", "security", "policy", "containers", "resources"):
        assert section in observed and "error" not in observed[section], (section, observed[section])
    assert observed["models"]["per_model"] and observed["database"]["tables"]
    assert observed["policy"]["recent"] and observed["containers"]["inference_runtime"]["reachable"]


def test_what_the_workbench_remembers_is_curated_by_people(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    stated = api.client.post("/api/memory", headers=engineer, json={
        "content": "Lathe L-1 sleeves for pump P-310 need 0.02 mm.", "subject": "Lathe L-1", "memory_type": "constraint"})
    assert stated.status_code == 201 and stated.json()["operation"] in ("create", "ignore", "update")
    found = api.client.get("/api/memory?q=pump sleeves tolerance", headers=engineer).json()
    mine = next(m for m in found["memories"] if "sleeves" in m["content"])
    assert mine["classification"] == "INTERNAL" and mine["acl"] == ["process-engineering"]
    assert api.client.put(f"/api/memory/{mine['id']}", headers=engineer,
                          json={"content": "Lathe L-1 sleeves for pump P-310 need 0.02 mm runout."}).status_code == 200
    other = _login(api, "demo-engineer-2")  # instrumentation: a different department
    assert not [m for m in api.client.get("/api/memory?q=pump sleeves", headers=other).json()["memories"]
                if m["id"] == mine["id"]]
    assert api.client.put(f"/api/memory/{mine['id']}", headers=other, json={"content": "hijacked text"}).status_code == 404
    assert api.client.post(f"/api/memory/{mine['id']}/status", headers=engineer, json={"status": "archived"}).status_code == 200
    archived = api.client.get("/api/memory?status=archived", headers=engineer).json()["memories"]
    assert any(m["id"] == mine["id"] for m in archived)
    events = api.client.get("/api/memory/events", headers=engineer).json()["events"]
    assert {"edit", "archive"} <= {e["operation"] for e in events}


def test_documents_live_in_folders_and_keep_their_versions(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    listing = api.client.get("/api/documents", headers=engineer).json()["documents"]
    folders = {d["folder"] for d in listing}
    assert {"COMPANY_POLICIES", "MANUFACTURING_DEPT/sector1", "PROCUREMENT_DEPT/sector1", "REFERENCE_LIBRARY"} <= folders
    assert "PROCUREMENT_DEPT/sector2" not in folders  # the confidential quotations are not this engineer's
    policy = next(d for d in listing if d["title"].startswith("Policy 1"))
    versions = api.client.get(f"/api/documents/{policy['id']}/versions", headers=engineer).json()["versions"]
    assert [v["version"] for v in versions] == [1, 2] and versions[1]["current"]
    diff = api.client.get(f"/api/documents/{policy['id']}/diff", headers=engineer).json()
    changed = " ".join(f"{c.get('before')} -> {c.get('after')}" for c in diff["changes"])
    assert "40%" in changed and "30%" in changed
    page = api.client.get(f"/api/documents/{policy['id']}/pages/1?version=1", headers=engineer).json()
    assert any("40%" in b["text"] for b in page["blocks"])
    files = {"file": ("shift-note.txt", b"Lathe L-1 tailstock re-aligned on 20 Sep 2026.", "text/plain")}
    uploaded = api.client.post("/api/documents", headers=engineer, files=files, data={
        "classification": "internal", "acl": "process-engineering", "folder": "MANUFACTURING_DEPT/sector1/notes"})
    assert uploaded.status_code == 201, uploaded.text
    bad = api.client.post("/api/documents", headers=engineer, files=files, data={
        "classification": "internal", "acl": "process-engineering", "folder": "../../etc"})
    assert bad.status_code == 400 and bad.json()["code"] == "folder_invalid"
