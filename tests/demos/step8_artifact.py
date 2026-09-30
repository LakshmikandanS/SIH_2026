"""Step 8 demo -- generate, verify, approve, release, shown rather than
asserted.

    .venv/Scripts/python -m tests.demos.step8_artifact

Submits one real task over HTTP exactly like step 7's demo (`hermes3` plans
it, the agent loop runs rag.search then python.execute through the real Tool
Gateway and a real Execution Service subprocess) -- but this time
`generate_report` is the REAL `app.artifact.backend`, not step 7's
placeholder: the report is the real `maintenance_summary_v1` template, the
Verifier's five structural checks actually run, and the task reaches
WAITING_FOR_APPROVAL with a genuine VERIFIED artifact and a REVIEW_REQUIRED
Approval row. Then an approver decides APPROVED through
`POST /approvals/{approval_id}/decision`, in one transaction, and the task
ends COMPLETED with the artifact RELEASED.

Needs a live Ollama (localhost:11434, `hermes3`) and a live Docker daemon --
the same two dependencies step 7's demo needs. Runs against a throwaway
database; it never touches var/citadel.db.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step8_demo.db"
_DEMO_DB.unlink(missing_ok=True)
os.environ["CITADEL_DATABASE_URL"] = f"sqlite:///{_DEMO_DB.as_posix()}"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.db.state_machines import Classification, Role  # noqa: E402
from app.identity import create_user  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability import format_trace, get_trace, verify_chain  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _docker_reachable() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_execution_service() -> tuple[subprocess.Popen, str]:
    """Launch the real, separate process (step 5) -- same pattern
    `tests/demos/step7_orchestrator.py` uses."""
    port = _free_port()
    env = os.environ.copy()
    env["CITADEL_EXECUTION_HOST"] = "127.0.0.1"
    env["CITADEL_EXECUTION_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "execution_service"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"execution_service exited early:\n{proc.stdout.read() if proc.stdout else ''}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return proc, base_url
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("execution_service never became healthy")


def main() -> None:
    if not _docker_reachable():
        print("Docker daemon is not reachable -- python.execute needs it (section 2). Start")
        print("Docker Desktop and re-run this demo.")
        return

    exec_proc, exec_url = start_execution_service()
    os.environ["CITADEL_EXECUTION_SERVICE_URL"] = exec_url
    print(f"Execution Service started as a real subprocess at {exec_url}")

    try:
        _run_demo()
    finally:
        exec_proc.terminate()
        try:
            exec_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exec_proc.kill()
        print("\nExecution Service subprocess stopped.")


def _run_demo() -> None:
    init_db.reset()
    with SessionLocal() as session:
        create_user(
            session,
            username="j.rao",
            password="engineer-pw",
            roles=[Role.ENGINEER],
            clearance=Classification.CONFIDENTIAL,
            department="maintenance",
            user_id="U123",
        )
        create_user(
            session,
            username="a.singh",
            password="approver-pw",
            roles=[Role.APPROVER],
            clearance=Classification.CONFIDENTIAL,
            department="maintenance",
            user_id="U-APPROVER",
        )
        session.commit()

    rule("1. Login as the engineer (section 1.1 step 1)")
    with TestClient(create_app()) as client:
        login = client.post("/login", json={"username": "j.rao", "password": "engineer-pw"})
        token = login.json()["access_token"]
        print(f"  POST /login -> HTTP {login.status_code}, session JWT cached")
        headers = {"Authorization": f"Bearer {token}"}

        rule("2. Submit the task -- plan, agent loop, generate_report (section 1.1 steps 2-13)")
        print('  /task "Using the available internal maintenance documents, identify')
        print('  the recent maintenance history of Pump P-101 and generate a short')
        print('  maintenance summary report." --classification CONFIDENTIAL\n')
        print("  This is the real maintenance_summary_v1 template now (app.artifact.backend),")
        print("  not step 7's placeholder -- this can take 10-30s: a real model call plus a")
        print("  real container run.\n")

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
            headers=headers,
        )
        result = response.json()
        task_id = result["task_id"]
        artifact_id = result.get("artifact_id")
        print(f"  POST /task -> HTTP {response.status_code}")
        print(f"  task_id       : {task_id}")
        print(f"  status        : {result['status']}")
        print(f"  artifact_id   : {artifact_id}")

        if result["status"] != "WAITING_FOR_APPROVAL":
            print(f"\n  Task ended {result['status']}: {result['reason']}")
            return

        rule("3. The Verifier's five checks (section 6.10, BB-043)")
        trace = client.get(f"/tasks/{task_id}/trace", headers=headers).json()
        verified_event = next(
            e for e in trace["events"] if e["event_type"] == "ARTIFACT_VERIFIED"
        )
        checks = verified_event["payload"]
        for name in (
            "file_exists_and_readable",
            "sha256_computed",
            "has_required_sections",
            "evidence_classification_ok",
            "has_provenance",
        ):
            print(f"  {name:<28} {checks[name]}")
        print(f"  {'ALL PASSED' if checks['passed'] else 'FAILED'} -- sha256={checks['sha256']}")

        approval_event = next(
            e for e in trace["events"] if e["event_type"] == "APPROVAL_REQUESTED"
        )
        approval_id = approval_event["payload"]["approval_id"]
        print(f"\n  Approval {approval_id} created at REVIEW_REQUIRED.")

        rule("4. Human approves (section 1.1 step 16) -- approver_id from the session JWT")
        approver_login = client.post(
            "/login", json={"username": "a.singh", "password": "approver-pw"}
        )
        approver_headers = {"Authorization": f"Bearer {approver_login.json()['access_token']}"}

        decision = client.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "APPROVED", "comment": "Looks correct."},
            headers=approver_headers,
        )
        decision_body = decision.json()
        print(f"  POST /approvals/{approval_id}/decision -> HTTP {decision.status_code}")
        print(f"  artifact_status : {decision_body['artifact_status']}")
        print(f"  task_status     : {decision_body['task_status']}")

        rule("5. Final state (section 1.1 step 17) -- one transaction, terminal")
        status_resp = client.get(f"/tasks/{task_id}", headers=headers)
        print(f"  GET /tasks/{task_id} -> {status_resp.json()['status']}")

        rule("6. The trace (section 6.12)")
        final_trace = client.get(f"/tasks/{task_id}/trace", headers=headers)
        print(format_trace(get_trace(task_id)))

    rule("7. The hash chain")
    verify_chain()
    events = get_trace(None)
    print(f"  {len(events)} events, chain verified unbroken.")
    types_seen = sorted({e.event_type for e in events})
    print(f"  event types this run touched: {', '.join(types_seen)}")


if __name__ == "__main__":
    main()
