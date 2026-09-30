"""Step 7 demo -- task intake, real planning, and the agent loop, shown
rather than asserted.

    .venv/Scripts/python -m tests.demos.step7_orchestrator

Submits one real task over HTTP, exactly as the CLI's `/task` will (step 9):
`hermes3` plans it, the agent loop executes rag.search then python.execute
through the real Tool Gateway, and the task reaches WAITING_FOR_APPROVAL
behind a placeholder generate_report backend -- step 8's real one replaces it
without changing this call. Then prints the trace and verifies the chain.

Needs a live Ollama (localhost:11434, `hermes3`) and a live Docker daemon --
the same two dependencies steps 6 and 5 already demoed for real.

Runs against a throwaway database; it never touches var/citadel.db.
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

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step7_demo.db"
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
    """Launch the real, separate process (step 5) -- not an in-process
    stand-in. Same pattern `tests/demos/step5_execution.py` uses."""
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
        print("Docker daemon is not reachable -- python.execute needs it (§2). Start")
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
        session.commit()

    rule("1. Login (§1.1 step 1)")
    with TestClient(create_app()) as client:
        login = client.post("/login", json={"username": "j.rao", "password": "engineer-pw"})
        token = login.json()["access_token"]
        print(f"  POST /login -> HTTP {login.status_code}, session JWT cached")
        headers = {"Authorization": f"Bearer {token}"}

        rule("2. Submit the task (§1.1 steps 2-6)")
        print('  /task "Using the available internal maintenance documents, identify')
        print('  the recent maintenance history of Pump P-101 and generate a short')
        print('  maintenance summary report." --classification CONFIDENTIAL\n')
        print("  Query Router creates task_id, hands off to the Orchestrator, which")
        print("  plans it with hermes3 and runs the agent loop to completion --")
        print("  this can take 10-30s: a real model call plus a real container run.\n")

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
        print(f"  POST /task -> HTTP {response.status_code}")
        print(f"  task_id       : {task_id}")
        print(f"  status        : {result['status']}")
        print(f"  agent_status  : {result['agent_status']}")
        print(f"  artifact_id   : {result['artifact_id']}")

        rule("3. Task status (§6.2: the Orchestrator is the sole owner)")
        status_resp = client.get(f"/tasks/{task_id}", headers=headers)
        print(f"  GET /tasks/{task_id} -> {status_resp.json()['status']}")

        rule("4. The trace (§6.12) -- rag.search, then python.execute, then generate_report")
        trace = client.get(f"/tasks/{task_id}/trace", headers=headers)
        print(trace.json()["text"])

    rule("5. The hash chain")
    verify_chain()
    events = get_trace(None)
    print(f"  {len(events)} events, chain verified unbroken.")
    types_seen = sorted({e.event_type for e in events})
    print(f"  event types this run touched: {', '.join(types_seen)}")

    if result["status"] == "WAITING_FOR_APPROVAL":
        print("\n  Task reached WAITING_FOR_APPROVAL: plan generated, evidence retrieved")
        print("  through the real Tool Gateway, computation ran in a real sandbox")
        print("  container, and a placeholder artifact was produced. Step 8 replaces")
        print("  the placeholder generate_report backend with the real template,")
        print("  Verifier, and approval endpoint -- this call does not change.")
    else:
        print(f"\n  Task ended {result['status']}: {result['reason']}")


if __name__ == "__main__":
    main()
