"""Sovereignty telemetry, the fence and the probe.

Audit hooks cannot be removed once installed, so every test that installs them runs in
a fresh Python process; the parent only reads what the child printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from citadel_platform.db import Database
from citadel_sovereignty import EgressPolicy, HttpSandboxRunner, enforcement_status, status, task_report
from citadel_sovereignty.telemetry import ConnectionScanner, EgressRecorder
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector

REPO_ROOT = Path(__file__).resolve().parents[3]


def _child(script: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    full_env = {**os.environ, **(env or {})}
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True, timeout=120, env=full_env
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_the_policy_knows_the_deployments_own_destinations():
    policy = EgressPolicy.from_env(
        {"CITADEL_INFERENCE_ENDPOINT": "http://10.20.30.40:11434", "CITADEL_EGRESS_ALLOW": "db.internal:5432"}
    )
    assert policy.address_allowed("127.0.0.1") and policy.address_allowed("10.20.30.40", 11434)
    assert policy.name_allowed("localhost") and policy.name_allowed("db.internal")
    assert not policy.address_allowed("1.1.1.1") and not policy.name_allowed("example.com")


def test_the_monitor_sees_what_the_fence_refuses_and_the_fence_refuses_it():
    result = _child(
        """
        import json, socket
        from citadel_sovereignty import install, EgressBlocked
        s = install("test", None, scan=False, env={"CITADEL_INFERENCE_ENDPOINT": "http://127.0.0.1:9"})
        outcomes = {}
        for name, call in [
            ("external_connect", lambda: socket.create_connection(("10.255.255.1", 443), timeout=1)),
            ("external_lookup", lambda: socket.getaddrinfo("example.com", 443)),
            ("internal_connect", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1)),
        ]:
            try:
                call()
                outcomes[name] = "connected"
            except EgressBlocked as exc:
                outcomes[name] = "fenced"
            except OSError as exc:
                outcomes[name] = "os-error"
        print(json.dumps({"outcomes": outcomes, "counts": s.recorder.counts(), "refusals": s.fence.refusals}))
        """
    )
    assert result["outcomes"] == {"external_connect": "fenced", "external_lookup": "fenced", "internal_connect": "os-error"}
    # the monitor recorded both attempts before the fence refused them
    assert result["counts"]["attempt"] == 2
    assert result["counts"]["blocked"] == 1 and result["counts"]["dns_denied"] == 1
    assert result["refusals"] == 2


def test_the_probe_steps_around_the_fence_and_reports_what_the_network_did():
    result = _child(
        """
        import json
        from citadel_sovereignty import install, run_probe
        s = install("probe-test", None, scan=False, env={})
        report = run_probe(s.recorder, targets=[("192.0.2.1", 9)], timeout_s=1.0)
        print(json.dumps({"report": report, "counts": s.recorder.counts(), "refusals": s.fence.refusals}))
        """
    )
    assert result["refusals"] == 0  # the probe tested the network, not the fence
    assert result["counts"]["attempt"] == 1  # but the monitor still saw it
    outcome = result["report"]["results"][0]["outcome"]
    assert outcome in ("blocked", "connected")
    assert result["report"]["egress_blocked"] == (outcome != "connected")


def test_outside_the_container_the_report_says_there_is_no_network_enforcement(tmp_path: Path):
    missing = enforcement_status({"CITADEL_ENFORCEMENT_FILE": str(tmp_path / "none.json")})
    assert missing["applied"] is False and "in-process fence" in missing["note"]
    (tmp_path / "e.json").write_text(json.dumps({"mechanism": "nftables", "applied": True}))
    assert enforcement_status({"CITADEL_ENFORCEMENT_FILE": str(tmp_path / "e.json")})["applied"] is True


@requires_pgvector
@pytest.mark.integration
def test_the_task_report_attributes_and_counts():
    with pg_scratch_db() as env:
        apply_all_migrations(env)
        db = Database(env=env)
        task_id = str(db.scalar(
            "INSERT INTO tasks (goal, submitted_by, classification) VALUES ('t', "
            "(SELECT id FROM users WHERE external_identity = 'demo-engineer-1'), 'internal') RETURNING id::text"
        ))
        from citadel_sovereignty import EgressRecorder

        recorder = EgressRecorder(db, "worker")
        recorder.record("attempt", "monitor", "1.1.1.1", 443, task_id=task_id, agent_id="agent-1")
        recorder.record("blocked", "probe", "1.1.1.1", 443, {"outcome": "blocked"}, task_id=task_id, agent_id="agent-1")
        recorder.flush()
        report = task_report(db, task_id)
        assert report["counts"]["attempts"] == 1 and report["counts"]["blocked"] == 1 and report["counts"]["observed"] == 0
        assert report["events"][0]["agent_id"] == "agent-1"
        assert report["verdict"].startswith("No data left the deployment")
        assert status(db)["counts"]["probes"] == 1


def test_the_sandbox_client_never_honours_an_ambient_proxy(monkeypatch: pytest.MonkeyPatch):
    """The workspace and its receipt go to the sandbox on an internal network and nowhere
    else. The proxy set here is a dead port: had the client honoured it, /health could not
    have answered."""
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"ok": True, "service": "citadel-sandbox"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        health = HttpSandboxRunner(f"http://127.0.0.1:{server.server_address[1]}").health()
        assert health == {"ok": True, "service": "citadel-sandbox"}
    finally:
        server.shutdown()


def test_the_scanner_counts_what_this_process_opened_not_what_connected_to_it():
    """Inbound is not egress. Docker hands the API its browser connections from an address
    outside every internal network; the scanner must not report the operator's own browser
    as data leaving -- while a connection this process opens itself still counts. The
    policy here trusts nothing, not even loopback, so both ends of one local connection
    are "external" and only the direction tells them apart."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    outbound = socket.create_connection(listener.getsockname(), timeout=5)
    accepted, _ = listener.accept()
    try:
        scanner = ConnectionScanner(EgressPolicy(), EgressRecorder(None, "test"))
        external = scanner.scan()
        ours = [c for c in external if c["port"] == listener.getsockname()[1]]
        inbound = [c for c in external if c["port"] == outbound.getsockname()[1]]
        assert len(ours) == 1, external          # the connection this process opened
        assert inbound == [], external           # the one it accepted is not egress
    finally:
        for s_ in (accepted, outbound, listener):
            s_.close()
