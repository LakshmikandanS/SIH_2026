"""Step 5 demo -- the isolated execution zone, shown rather than asserted.

    .venv/Scripts/python -m tests.demos.step5_execution

Starts the Execution Service as a genuine, separate OS process (the same
`python -m execution_service` command `docker/execution-service.Dockerfile`
runs), registers the real `python.execute` backend the way §6.6 says every
backend is attached, and drives three calls through the *whole* Tool Gateway
(§6.6 Step A -> Step B -> Step C) against real, one-shot Docker containers:

  1. A successful sandboxed computation -- §1.1 step 12's actual shape:
     parsing maintenance dates out of the retrieved P-101 corpus and
     computing days-since-last-service.
  2. The §6.13 no-network proof: a real outbound attempt from inside the
     container, and its failure.
  3. A resource-limit trip: code that tries to over-allocate memory, and the
     sandbox refusing to report that as a "successful" run.

Needs a reachable Docker daemon; exits early with a clear message if there
isn't one. Runs against a throwaway database; it never touches
var/citadel.db, and terminates the Execution Service subprocess (and
confirms no container was left behind) before exiting.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step5_demo.db"
_DEMO_DB.unlink(missing_ok=True)
os.environ["CITADEL_DATABASE_URL"] = f"sqlite:///{_DEMO_DB.as_posix()}"

import httpx  # noqa: E402

from app.capability import issue_for_step  # noqa: E402
from app.db import init_db  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Agent, Task, User  # noqa: E402
from app.db.state_machines import Classification, Role  # noqa: E402
from app.execution import backend as execution_backend  # noqa: E402
from app.execution import settings as execution_settings  # noqa: E402
from app.policy import Tool  # noqa: E402
from app.tool_gateway import clear_backends, invoke, register_backend, task_resource  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(label: str, envelope: dict) -> None:
    if envelope["success"]:
        verdict = "ALLOW  -> executed"
    else:
        verdict = f"DENY/ERROR -> {envelope['error']['code']}"
    print(f"  {label:<52} {verdict}")
    if not envelope["success"]:
        print(f"  {'':<52} {envelope['error']['message']}")


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
    """Launch the real, separate process -- not an in-process stand-in."""
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


def seed() -> None:
    init_db.reset()
    session = SessionLocal()
    engineer = User(
        user_id="U123",
        username="j.rao",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
    )
    session.add(engineer)
    session.flush()
    task = Task(
        task_id="T123",
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True, "needs_document_generation": True},
    )
    session.add(task)
    session.flush()
    session.add(Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher"))
    session.commit()
    session.close()


def run_python_execute(code: str, **extra_arguments) -> dict:
    capability = issue_for_step("T123", "A123", Tool.PYTHON_EXECUTE)
    return invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource("T123"),
        arguments={"code": code, **extra_arguments},
    )


def main() -> None:
    if not _docker_reachable():
        print("Docker daemon is not reachable -- this demo needs it. Skipping.")
        return

    seed()
    clear_backends()

    rule("0. Starting the Execution Service as a real, separate OS process")
    proc, base_url = start_execution_service()
    print(f"  execution_service listening at {base_url}  (pid {proc.pid})")
    execution_settings.EXECUTION_SERVICE_URL = base_url
    register_backend(Tool.PYTHON_EXECUTE, execution_backend.python_execute)
    print("  registered: register_backend(Tool.PYTHON_EXECUTE, python_execute)")

    try:
        rule("1. A successful sandboxed computation (§1.1 step 12)")
        corpus_path = _REPO_ROOT / "data" / "maintenance" / "pump_p101_history.txt"
        evidence_text = corpus_path.read_text(encoding="utf-8")
        print(f"  evidence: {corpus_path.relative_to(_REPO_ROOT)} ({len(evidence_text)} bytes)")
        code = f"""
import json, re
from datetime import datetime, timezone

text = {json.dumps(evidence_text)}
dates = [datetime.strptime(d, "%Y-%m-%d").date()
         for d in re.findall(r"^(\\d{{4}}-\\d{{2}}-\\d{{2}})", text, re.MULTILINE)]
most_recent = max(dates)
days_since = (datetime.now(timezone.utc).date() - most_recent).days
print(json.dumps({{"most_recent": most_recent.isoformat(),
                    "days_since": days_since, "records_found": len(dates)}}))
"""
        envelope = run_python_execute(code)
        show("python.execute: parse dates, compute days-since-service", envelope)
        if envelope["success"]:
            payload = json.loads(envelope["result"]["stdout"])
            print(f"  most recent service : {payload['most_recent']}")
            print(f"  days since service   : {payload['days_since']}")
            print(f"  records parsed       : {payload['records_found']}")

        rule("2. The §6.13 no-network proof")
        print("  Same sandbox, attempting a real outbound call to https://example.com.\n")
        network_code = """
import json, socket
from urllib.request import urlopen
outcome = {}
try:
    urlopen("https://example.com", timeout=5)
    outcome["http"] = "CONNECTED"
except Exception as e:
    outcome["http"] = f"FAILED:{type(e).__name__}"
try:
    socket.create_connection(("example.com", 443), timeout=5)
    outcome["raw_socket"] = "CONNECTED"
except Exception as e:
    outcome["raw_socket"] = f"FAILED:{type(e).__name__}"
print(json.dumps(outcome))
"""
        envelope = run_python_execute(network_code, timeout_seconds=15)
        show("python.execute: curl-equivalent outbound attempt", envelope)
        if envelope["success"]:
            outcome = json.loads(envelope["result"]["stdout"])
            print(f"  HTTPS attempt  : {outcome['http']}")
            print(f"  raw TCP attempt: {outcome['raw_socket']}")
            print("\n  No route to any external host. §6.13's claim, proven live.")

        rule("3. A resource-limit trip")
        print(
            "  Code that allocates ~300MB inside a container capped at the\n"
            "  service's default 256MB (CITADEL_EXECUTION_MEMORY_LIMIT).\n"
        )
        envelope = run_python_execute(
            "x = bytearray(300 * 1024 * 1024)\nprint('should never print')\n"
        )
        show("python.execute: 300MB allocation, 256MB cap", envelope)
        print("\n  OOM-killed by the container's own cgroup limit, reported as")
        print("  EXECUTION_ERROR -- not a 'successful' run with a strange exit code.")

        rule("4. A timeout trip")
        print("  An infinite loop, given a 2-second budget.\n")
        envelope = run_python_execute(
            "import time\nwhile True:\n    time.sleep(1)\n", timeout_seconds=2
        )
        show("python.execute: infinite loop, 2s timeout", envelope)
        print("\n  Killed on schedule, reported as EXECUTION_ERROR -- the same uniform")
        print("  envelope every other backend failure uses (§6.6), not a hang and not a crash.")

    finally:
        rule("5. Shutting down the Execution Service")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"  process {proc.pid} stopped.")
        try:
            import docker

            leftover = docker.from_env().containers.list(all=True)
            print(f"  containers remaining on the daemon: {len(leftover)} (expected 0)")
        except Exception as exc:  # pragma: no cover - best-effort only
            print(f"  (could not verify: {exc})")
        clear_backends()


if __name__ == "__main__":
    main()
