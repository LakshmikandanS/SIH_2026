"""Design doc §2, §6.6, §6.13 -- the Execution Service's own contract.

These are live-Docker tests: they spawn `execution_service` as a real,
separate OS process (`python -m execution_service`, exactly as
`docker/execution-service.Dockerfile` runs it) and let it create real,
one-shot containers against the host's Docker daemon. Nothing here is
simulated -- if the daemon can't be reached, every test in this module skips
with a clear reason rather than failing (`docker_available` below); with the
daemon running, they must pass.

    [x] A python.execute call runs arbitrary user code and returns
        stdout/stderr/exit_code correctly (live container round-trip)
    [x] §6.13 -- the execution-zone containers have no route to any external
        host, proven by an outbound call from inside a python.execute
        container that must fail
    [x] A timeout / resource-limit case is killed rather than left running
    [x] An execution failure surfaces as EXECUTION_ERROR through the full
        Tool Gateway envelope (§6.6), not a raw exception
    [x] The container is destroyed immediately after the call returns --
        never reused, never left running

§6.13 names `curl https://example.com` as the illustrative check. The
sandbox image is the doc's own choice, plain `python:3.12-slim` (§2/§8 step
5), which does not ship a `curl` binary and has no network route to install
one -- so the proof below issues the equivalent real outbound HTTPS request
with Python's own standard library (`urllib.request`) instead, plus a raw
TCP connection attempt as a second, lower-level proof. Both are genuine
attempts to reach `example.com` from inside the sandbox; what §6.13 actually
asks to be demonstrated -- that the attempt fails -- is unchanged by which
HTTP client issues it.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from app.db.models import Agent

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def agent(db, task):
    """The one Agent per task (§3, BB-014) -- same shape as
    `tests/test_security.py`'s fixture of the same name."""
    row = Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher")
    db.add(row)
    db.commit()
    return row

# --------------------------------------------------------------------------
# Docker availability -- every live test in this module depends on this.
# --------------------------------------------------------------------------


def _docker_reachable() -> bool:
    try:
        import docker  # the ONE place outside execution_service/ this test
        # file is allowed to import it from -- tests/ is not app/, and this
        # check exists purely to decide whether to skip.

        docker.from_env().ping()
        return True
    except Exception:
        return False


_DOCKER_UP = _docker_reachable()

pytestmark = pytest.mark.skipif(
    not _DOCKER_UP,
    reason="Docker daemon is not reachable; the execution-service live tests need it",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running_containers() -> set[str]:
    """Sandbox containers only -- never every container on the daemon.

    The assertion these feed is "the one-shot container was destroyed". Diffing
    the daemon's whole container list answers a different, wrong question: it
    also fails when anything unrelated starts or stops mid-test, including a
    second pytest process or a container the developer happened to launch.
    """
    import docker

    from execution_service.sandbox import CONTAINER_LABELS

    client = docker.from_env()
    label_filter = [f"{k}={v}" for k, v in CONTAINER_LABELS.items()]
    return {c.id for c in client.containers.list(all=True, filters={"label": label_filter})}


# --------------------------------------------------------------------------
# The Execution Service as a real, separate OS process.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def execution_service_url() -> Iterator[str]:
    """Start `python -m execution_service` as a genuine subprocess -- the
    same command `docker/execution-service.Dockerfile` runs -- and wait for
    it to answer `/health`. Torn down at module end; asserts it leaves no
    container behind on exit."""
    port = _free_port()
    env = os.environ.copy()
    env["CITADEL_EXECUTION_HOST"] = "127.0.0.1"
    env["CITADEL_EXECUTION_PORT"] = str(port)

    before = _running_containers()

    proc = subprocess.Popen(
        [sys.executable, "-m", "execution_service"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    import httpx

    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                f"execution_service exited early (code {proc.returncode}):\n{output}"
            )
        try:
            response = httpx.get(f"{base_url}/health", timeout=1)
            if response.status_code == 200:
                break
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError(f"execution_service never became healthy: {last_error}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    after = _running_containers()
    assert after == before, (
        "the Execution Service left containers behind after shutdown: "
        f"{after - before}"
    )


@pytest.fixture
def python_execute_backend(execution_service_url, monkeypatch):
    """Point the trusted zone's HTTP client at the module-scoped live
    service, then register the real backend exactly the way the design doc
    says every backend is attached (§6.6)."""
    from app.execution import backend as execution_backend
    from app.execution import settings as execution_settings
    from app.policy import Tool
    from app.tool_gateway import clear_backends, register_backend

    monkeypatch.setattr(execution_settings, "EXECUTION_SERVICE_URL", execution_service_url)
    register_backend(Tool.PYTHON_EXECUTE, execution_backend.python_execute)
    yield execution_backend.python_execute
    clear_backends()


# --------------------------------------------------------------------------
# Live container round-trip
# --------------------------------------------------------------------------


def test_python_execute_runs_real_code_and_returns_stdout_stderr_exit_code(
    db, task, agent, python_execute_backend
):
    """A python.execute call runs arbitrary user code and returns
    stdout/stderr/exit_code correctly -- through the *whole* Tool Gateway
    (§6.6 Step A -> Step B -> Step C), not just the sandbox in isolation."""
    from app.capability import issue_for_step
    from app.policy import Tool
    from app.tool_gateway import ErrorCode, invoke, task_resource

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={
            "code": "import sys\nprint('citadel-sandbox-ok')\nprint('oops', file=sys.stderr)\n"
        },
    )

    assert result["success"] is True, result
    assert result["error"] is None
    assert result["result"]["stdout"].strip() == "citadel-sandbox-ok"
    assert result["result"]["stderr"].strip() == "oops"
    assert result["result"]["exit_code"] == 0


def test_python_execute_returns_a_nonzero_exit_code_as_data_not_a_failure(
    db, task, agent, python_execute_backend
):
    """The caller's own code failing is a normal result, not an
    EXECUTION_ERROR -- only an infrastructure failure (timeout, resource
    limit, crash) is that (see the sandbox docstring)."""
    from app.capability import issue_for_step
    from app.policy import Tool
    from app.tool_gateway import invoke, task_resource

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)
    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={"code": "raise ValueError('deliberate failure')\n"},
    )

    assert result["success"] is True
    assert result["result"]["exit_code"] != 0
    assert "ValueError" in result["result"]["stderr"]


# --------------------------------------------------------------------------
# §1.1 step 12 / §6.7's real use: compute days-since-last-service from the
# retrieved maintenance corpus.
# --------------------------------------------------------------------------


def test_python_execute_computes_days_since_last_service_from_retrieved_text(
    db, task, agent, python_execute_backend
):
    """The demo's actual shape: parse maintenance dates out of retrieved
    text, find the most recent one, compute days-since. Reads the corpus
    directly off disk (this step does not own RAG retrieval -- step 6 does)
    to stand in for "evidence already retrieved by rag.search"."""
    from app.capability import issue_for_step
    from app.policy import Tool
    from app.tool_gateway import invoke, task_resource

    corpus_path = _REPO_ROOT / "data" / "maintenance" / "pump_p101_history.txt"
    evidence_text = corpus_path.read_text(encoding="utf-8")

    code = f"""
import json, re
from datetime import date, datetime, timezone

text = {json.dumps(evidence_text)}
dates = re.findall(r"^(\\d{{4}}-\\d{{2}}-\\d{{2}})", text, re.MULTILINE)
parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
most_recent = max(parsed)
days_since = (datetime.now(timezone.utc).date() - most_recent).days
print(json.dumps({{
    "most_recent": most_recent.isoformat(),
    "days_since": days_since,
    "records_found": len(parsed),
}}))
"""

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)
    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={"code": code},
    )

    assert result["success"] is True, result
    assert result["result"]["exit_code"] == 0, result["result"]["stderr"]
    payload = json.loads(result["result"]["stdout"])

    assert payload["most_recent"] == "2026-06-14"
    assert payload["records_found"] >= 5

    expected_days = (datetime.now(timezone.utc).date() - date(2026, 6, 14)).days
    # Allow a 1-day slack for a midnight boundary between the host clock
    # read above and the (UTC, per the base image's unset TZ) container's.
    assert abs(payload["days_since"] - expected_days) <= 1
    assert payload["days_since"] >= 0


# --------------------------------------------------------------------------
# §6.13 -- the mandatory network-isolation proof.
# --------------------------------------------------------------------------


def test_python_execute_container_has_no_route_to_any_external_host(
    db, task, agent, python_execute_backend
):
    """§6.13: "the execution-zone containers have no route to any external
    host", checked by "attempting curl https://example.com inside a
    python.execute container [...] and asserting failure". This is that
    test -- see the module docstring for why the request is issued with
    Python's stdlib instead of the `curl` binary the doc names."""
    from app.capability import issue_for_step
    from app.policy import Tool
    from app.tool_gateway import invoke, task_resource

    code = """
import json, socket
from urllib.request import urlopen

outcome = {"http_attempt": None, "raw_socket_attempt": None}

try:
    urlopen("https://example.com", timeout=5)
    outcome["http_attempt"] = "CONNECTED"
except Exception as e:
    outcome["http_attempt"] = f"FAILED:{type(e).__name__}"

try:
    socket.create_connection(("example.com", 443), timeout=5)
    outcome["raw_socket_attempt"] = "CONNECTED"
except Exception as e:
    outcome["raw_socket_attempt"] = f"FAILED:{type(e).__name__}"

print(json.dumps(outcome))
"""

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)
    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={"code": code, "timeout_seconds": 15},
    )

    assert result["success"] is True, result
    payload = json.loads(result["result"]["stdout"])

    # A real outbound attempt was made and failed both ways -- no DNS
    # resolution, no route, nothing to connect to. "CONNECTED" would mean
    # the whole isolation claim (§2, §6.13) is false.
    assert payload["http_attempt"].startswith("FAILED:"), payload
    assert payload["raw_socket_attempt"].startswith("FAILED:"), payload
    assert "CONNECTED" not in payload.values()


# --------------------------------------------------------------------------
# Timeout / resource-limit -- direct sandbox-module tests (still live
# Docker; bypasses the HTTP hop to isolate the mechanism under test).
# --------------------------------------------------------------------------


def test_a_runaway_container_is_killed_on_timeout():
    """A time limit, and a kill on timeout (§2, mission requirement 3)."""
    from execution_service import sandbox

    before = _running_containers()
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxError, match="time limit"):
        sandbox.run_python(
            "import time\nwhile True:\n    time.sleep(1)\n", timeout_seconds=2
        )
    elapsed = time.monotonic() - started

    # Killed close to the requested bound, not left running to the process's
    # own HTTP-client default.
    assert elapsed < 15
    after = _running_containers()
    assert after == before, "the timed-out container was not destroyed"


def test_a_memory_limit_violation_is_a_sandbox_error(monkeypatch):
    """A resource limit, and a violation of it is treated as an
    infrastructure failure -- not a "successful" run with a weird exit
    code (§2, mission requirement 3)."""
    from execution_service import sandbox
    from execution_service import settings as exec_settings

    monkeypatch.setattr(exec_settings, "SANDBOX_MEMORY_LIMIT", "16m")

    before = _running_containers()
    with pytest.raises(sandbox.SandboxError, match="memory limit"):
        sandbox.run_python("x = bytearray(300 * 1024 * 1024)\n", timeout_seconds=15)
    after = _running_containers()
    assert after == before, "the OOM-killed container was not destroyed"


# --------------------------------------------------------------------------
# An execution failure through the FULL gateway envelope (§6.6).
# --------------------------------------------------------------------------


def test_a_sandbox_timeout_surfaces_as_execution_error_through_the_gateway(
    db, task, agent, python_execute_backend
):
    """The one error code this backend is allowed to produce (§6.6): a
    timeout deep inside the isolated zone must still come back through
    `invoke()` as the same uniform envelope every other backend failure
    uses -- not a raised exception reaching the caller, and not a bespoke
    error code."""
    from app.capability import issue_for_step
    from app.observability import EventType, get_trace
    from app.policy import Tool
    from app.tool_gateway import ErrorCode, invoke, task_resource

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)
    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={
            "code": "import time\nwhile True:\n    time.sleep(1)\n",
            "timeout_seconds": 2,
        },
    )

    assert result["success"] is False
    assert result["result"] is None
    assert result["error"]["code"] == ErrorCode.EXECUTION_ERROR
    assert "time limit" in result["error"]["message"]

    # Recorded the same way step 4 proved every other backend failure is
    # recorded: TOOL_EXECUTED with success=false, never TOOL_DENIED --
    # authorization succeeded here, only the sandbox run itself failed.
    events = [e for e in get_trace(task.task_id) if e.event_type == EventType.TOOL_EXECUTED]
    assert events and events[-1].payload["success"] is False
    assert events[-1].payload["error"]["code"] == ErrorCode.EXECUTION_ERROR
    assert EventType.TOOL_DENIED not in [e.event_type for e in get_trace(task.task_id)]


def test_an_unreachable_execution_service_is_also_an_execution_error(
    db, task, agent, monkeypatch
):
    """If the isolated zone can't be reached at all, that is an
    EXECUTION_ERROR too -- not an unhandled connection exception leaking out
    of the Tool Gateway."""
    from app.capability import issue_for_step
    from app.execution import backend as execution_backend
    from app.execution import settings as execution_settings
    from app.policy import Tool
    from app.tool_gateway import ErrorCode, invoke, register_backend, task_resource

    monkeypatch.setattr(
        execution_settings, "EXECUTION_SERVICE_URL", "http://127.0.0.1:1"
    )
    register_backend(Tool.PYTHON_EXECUTE, execution_backend.python_execute)

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.PYTHON_EXECUTE)
    result = invoke(
        capability_token=capability.token,
        tool=Tool.PYTHON_EXECUTE,
        resource=task_resource(task.task_id),
        arguments={"code": "print('unreachable')"},
    )

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.EXECUTION_ERROR
    assert "could not reach" in result["error"]["message"]
