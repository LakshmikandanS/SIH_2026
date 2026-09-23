"""The sandbox service verifies before it runs, over its real HTTP surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from citadel_contracts.receipts import DecisionReceipt, new_decision_id, new_nonce, resource_digest, sign_receipt
from citadel_platform.keyring import init_keys, load_receipt_signing_key
from citadel_tools import code_resource


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_keys(tmp_path)
    monkeypatch.setenv("CITADEL_KEYS_DIR", str(tmp_path))
    from citadel_sandbox.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, load_receipt_signing_key({"CITADEL_KEYS_DIR": str(tmp_path)})


def _receipt(key, source: str) -> str:
    now = datetime.now(timezone.utc)
    return sign_receipt(DecisionReceipt(
        new_decision_id(), "00000000-0000-4000-8000-000000000001", "agent", "code.run",
        resource_digest(code_resource(source, "INTERNAL", ["process-engineering"])), {}, "allow-execute-engineer",
        now, now + timedelta(seconds=30), new_nonce(),
    ), key)


def test_runs_only_what_the_receipt_authorised(client):
    http, key = client
    source = "print(sum(range(5)))"
    request = {"source": source, "receipt": _receipt(key, source),
               "resource": {"classification": "INTERNAL", "acl": ["process-engineering"]}}
    ran = http.post("/run", json=request).json()
    assert ran["receipt_verified"] and ran["stdout"].strip() == "10" and ran["sandbox"]["kind"]
    replay = http.post("/run", json=request).json()
    assert replay["receipt_verified"] is False and "already been used" in replay["receipt_error"]
    swapped = http.post("/run", json={**request, "source": "print('other')", "receipt": _receipt(key, source)}).json()
    assert swapped["receipt_verified"] is False
    health = http.get("/health").json()
    assert health["runs"] == 1 and health["refused_receipts"] == 2


def test_the_probe_reports_an_outcome(client):
    http, _ = client
    report = http.post("/probe", json={"task_id": None}).json()
    assert report["process"] == "sandbox" and report["results"]
    assert all(r["outcome"] in ("blocked", "dns_denied", "connected") for r in report["results"])
