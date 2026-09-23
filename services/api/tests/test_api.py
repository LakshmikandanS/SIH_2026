"""The HTTP surface end to end: sign in, see only what you may, search with the denial
record, submit a task that a real worker runs, stream its journal over SSE, approve its
deliverable as a different person, download the released bytes, and read the routing
and sovereignty panels -- against a real scratch database and the ingested demo corpus.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
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


def test_the_ui_and_health_are_served_from_one_origin(api: SimpleNamespace):
    assert api.client.get("/api/health").json()["profile"] == "demo-local"
    page = api.client.get("/")
    assert page.status_code == 200 and "<html" in page.text.lower()


def test_protected_routes_need_a_verified_session(api: SimpleNamespace):
    assert api.client.get("/api/tasks").status_code == 401
    assert api.client.get("/api/documents", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_each_person_sees_their_corpus_and_the_denials_are_explained(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    listing = api.client.get("/api/documents", headers=engineer).json()["documents"]
    titles = " ".join(d["title"] for d in listing)
    assert "IR-2026-0147" in titles and "Incident" not in titles
    found = api.client.post("/api/search", headers=engineer, json={"query": "E-101 flange leak corrosion"}).json()
    assert found["hits"] and found["denied_count"] >= 1
    assert {d["reason"] for d in found["denied"]} and all("title" not in d for d in found["denied"])
    document = next(d for d in listing if "IR-2026-0147" in d["title"])
    page = api.client.get(f"/api/documents/{document['id']}/pages/2", headers=engineer).json()
    assert any("9.2" in b["text"] for b in page["blocks"])
    image = api.client.get(f"/api/documents/{document['id']}/pages/1/image", headers=engineer)
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    incident = api.client.get("/api/documents", headers=_login(api, "demo-approver")).json()["documents"]
    hidden = next(d for d in incident if "Incident" in d["title"])
    assert api.client.get(f"/api/documents/{hidden['id']}/pages/1", headers=engineer).status_code == 404


def test_the_upload_door_refuses_what_it_must(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    files = {"file": ("note.txt", b"E-101 shell temperature 180 C", "text/plain")}
    ok = api.client.post("/api/documents", headers=engineer, files=files,
                         data={"classification": "internal", "acl": "process-engineering", "title": "Shell temperature"})
    assert ok.status_code == 201, ok.text
    above = api.client.post("/api/documents", headers=engineer, files=files,
                            data={"classification": "confidential", "acl": "process-engineering"})
    assert above.status_code == 400 and above.json()["code"] == "exceeds_uploader_clearance"
    missing = api.client.post("/api/documents", headers=engineer, files=files, data={"acl": "process-engineering"})
    assert missing.json()["code"] == "classification_missing"


def test_a_task_runs_in_the_worker_streams_and_is_released_by_an_approver(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    created = api.client.post("/api/tasks", headers=engineer, json={
        "goal": "Prepare an approval note for heat exchanger E-101 from the latest inspection.",
        "user_id": "demo-approver",  # ignored: identity comes from the token
    })
    assert created.status_code == 201
    task = created.json()
    assert task["submitted_by"] == "demo-engineer-1" and task["status"] == "submitted"
    assert Worker(api.runtime, "api-test").run_once() == "awaiting_approval"

    events: list[dict[str, Any]] = []
    with api.client.stream("GET", f"/api/tasks/{task['id']}/events", headers=engineer) as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
            if line.startswith("event: end"):
                break
    kinds = [e.get("step_type") for e in events if "step_type" in e]
    assert kinds[0] == "submitted" and "tool_result" in kinds and kinds[-1] == "finished"

    detail = api.client.get(f"/api/tasks/{task['id']}", headers=engineer).json()
    artifact = next(a for a in detail["artifacts"] if a["requires_approval"])
    assert detail["evidence"] and artifact["status"] == "VERIFIED"
    preview = api.client.get(f"/api/artifacts/{artifact['id']}/preview", headers=engineer)
    assert "APPROVAL NOTE" in preview.text

    # the engineer cannot approve their own deliverable; the approver can
    assert api.client.post(f"/api/artifacts/{artifact['id']}/decision", headers=engineer,
                           json={"approve": True}).status_code == 403
    approver = _login(api, "demo-approver")
    pending = api.client.get("/api/artifacts?awaiting=1", headers=approver).json()["artifacts"]
    assert any(a["id"] == artifact["id"] for a in pending)
    decided = api.client.post(f"/api/artifacts/{artifact['id']}/decision", headers=approver,
                              json={"approve": True, "comment": "Agreed."})
    assert decided.status_code == 200, decided.text
    assert decided.json()["outcome"]["status"] == "RELEASED" and decided.json()["task"]["status"] == "completed"
    download = api.client.get(f"/api/artifacts/{artifact['id']}/download", headers=engineer)
    assert hashlib.sha256(download.content).hexdigest() == download.headers["x-citadel-sha256"]
    assert download.headers["x-citadel-classification"] == "INTERNAL"
    provenance = api.client.get(f"/api/artifacts/{artifact['id']}/provenance", headers=engineer).json()
    assert provenance["approvals"][0]["approver"] == "demo-approver"
    report = api.client.get(f"/api/tasks/{task['id']}/sovereignty", headers=engineer).json()
    assert report["counts"]["observed"] == 0


def test_the_routing_panel_explains_every_choice(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    preview = api.client.post("/api/routing/preview", headers=engineer, json={}).json()
    by_label = {s["label"]: s for s in preview["scenarios"]}
    code = by_label["Acting on a code task"]["decision"]
    plan = by_label["Planning a task"]["decision"]
    assert code["selected"] != plan["selected"]
    assert "task fit" in code["reason"]
    assert all(c["summary"] for c in by_label["Re-reading a scanned region"]["candidates"])
    status = api.client.get("/api/models", headers=engineer).json()
    assert status["reachable"] and not status["missing"]


def test_the_sovereignty_panel_and_the_approver_cannot_pull_models(api: SimpleNamespace):
    approver = _login(api, "demo-approver")
    assert api.client.post("/api/models/pull", headers=approver, json={}).status_code == 403
    panel = api.client.get("/api/sovereignty", headers=approver).json()
    assert "counts" in panel["status"] and panel["enforcement"]["applied"] in (True, False)
    assert panel["sandbox"]["kind"] == "process"


def test_the_policy_try_surface_uses_the_real_rules(api: SimpleNamespace):
    engineer = _login(api, "demo-engineer-1")
    denied = api.client.post("/api/policy/try", headers=engineer, json={
        "tool": "docs.read", "resource": {"classification": "confidential", "acl": ["process-engineering"]},
    }).json()
    assert denied["decision"]["rule_id"] == "deny-above-clearance"
    allowed = api.client.post("/api/policy/try", headers=engineer, json={
        "tool": "docs.read", "resource": {"classification": "internal", "acl": ["process-engineering"]},
    }).json()
    assert allowed["decision"]["effect"] == "allow" and "receipt" in allowed["receipt"]


def test_workspace_paths_cannot_escape(api: SimpleNamespace, tmp_path: Path):
    engineer = _login(api, "demo-engineer-1")
    task = api.client.post("/api/tasks", headers=engineer, json={"goal": "Summarise the pump history."}).json()
    assert api.client.get(f"/api/tasks/{task['id']}/workspace/..%2F..%2Fkeys%2Freceipt.key", headers=engineer).status_code in (400, 404)
    assert api.client.post(f"/api/tasks/{task['id']}/cancel", headers=engineer).json()["status"] == "cancelled"
