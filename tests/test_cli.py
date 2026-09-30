"""Design doc section 1 / section 6.1 -- the `citadel` CLI. Owned by `cli`
(step 9).

Every command in `cli/main.py` is a thin wrapper around one of the six real
HTTP endpoints steps 4/7/8 already built and tested; nothing here re-tests
identity, policy, orchestration or approval logic -- that is `test_security`/
`test_orchestration`/`test_artifact`'s job. What this module proves is that
the CLI's own layer (session caching across invocations, error rendering,
`--decision`/task-id-or-approval-id resolution, trace rendering) actually
drives that HTTP surface correctly.

Following `tests/test_artifact.py`'s own discipline: `TestClient(create_app())`
is used *without* `with` for everything that does not need the real tool
backends (login, admin, trace) -- that avoids the FastAPI startup hook, which
would otherwise try to ingest the demo RAG corpus through a real Ollama call
(docs/BUILD_LOG.md's own note on `app.main`'s startup hook). The one test that
drives a full task through the CLI stubs the planner and binds fake
`rag.search`/`python.execute` backends, exactly like `test_artifact.py`'s
`_register_fake_backends`/`_stub_generate_plan`, so it needs neither Ollama
nor Docker. A separate, explicitly live test exercises the real thing end to
end and skips cleanly if either is unreachable.
"""

from __future__ import annotations

import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# The CLI's session cache must never touch the real developer's
# ~/.citadel/session.json -- point it at a throwaway directory before `cli.*`
# is imported anywhere (module-level constants are read once, at import
# time), the same discipline tests/conftest.py uses for CITADEL_DATABASE_URL.
_CLI_HOME = Path(tempfile.gettempdir()) / f"citadel_cli_test_{os.getpid()}"
os.environ["CITADEL_CLI_HOME"] = str(_CLI_HOME)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from app.artifact.backend import register as register_report_backend  # noqa: E402
from app.db.models import Agent  # noqa: E402
from app.db.state_machines import Classification, Role, TaskStatus  # noqa: E402
from app.identity.service import create_user  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability import EventType, append_event  # noqa: E402
from app.orchestrator.plan import PlanGenerationResult  # noqa: E402
from app.orchestrator.schemas import PlanModel, PlanStepModel  # noqa: E402
from app.policy.tools import Tool  # noqa: E402
from app.policy.tool_disabled import get_registry as get_tool_disabled_registry  # noqa: E402
from app.tool_gateway import clear_backends, register_backend  # noqa: E402

import cli.main as cli_main  # noqa: E402
import cli.session as cli_session  # noqa: E402
from cli.client import CitadelClient  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cli_and_gateway_state():
    """Three independent pieces of module-level global state, reset around
    every test: the CLI's on-disk session cache (so one test's `login` can
    never leak into another's), the Tool Gateway's backend registry
    (`tests/test_orchestration.py`'s own fixture of the same shape), and the
    `tool_disabled` emergency-control registry -- `test_admin_disable_tool_
    succeeds_for_admin` really does flip `python.execute` off for the whole
    process (§6.8's own "one-way switch"), so it must not leak into a later
    test's task run."""
    cli_session.clear()
    clear_backends()
    get_tool_disabled_registry().clear()
    yield
    cli_session.clear()
    clear_backends()
    get_tool_disabled_registry().clear()


def _field(output: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*:\s*(.+)$", output, re.MULTILINE)
    assert match, f"no {name!r} field in CLI output:\n{output}"
    return match.group(1).strip()


def _bind_client(client: TestClient) -> None:
    """Point every CLI command at this one shared `TestClient` instance
    instead of opening a real socket -- the seam `cli.main._make_client`
    exists for."""
    cli_main._make_client = lambda base_url: CitadelClient(base_url, http_client=client)


@pytest.fixture
def engineer(db):
    user = create_user(
        db,
        username="j.rao",
        password="engineer-pw",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
        user_id="U123",
    )
    db.commit()
    return user


@pytest.fixture
def approver(db):
    user = create_user(
        db,
        username="a.singh",
        password="approver-pw",
        roles=[Role.APPROVER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
        user_id="U-APPROVER",
    )
    db.commit()
    return user


# ---------------------------------------------------------------------------
# "cover at minimum": login caching, admin role-gating, trace rendering.
# ---------------------------------------------------------------------------


def test_login_caches_a_token_a_later_command_can_read(db, engineer):
    """section 1.1 step 1, made concrete: `citadel login` in one process,
    `citadel status` in a *later* one (simulated here by a second, separate
    `runner.invoke` call reading the same on-disk cache) must see the
    session -- an in-memory-only cache would fail this."""
    client = TestClient(create_app())
    _bind_client(client)

    assert not cli_session.config.SESSION_PATH.exists()

    result = runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", "http://testserver"],
    )
    assert result.exit_code == 0, result.output
    assert "Logged in as j.rao" in result.output
    assert cli_session.config.SESSION_PATH.exists()

    cached = cli_session.load()
    assert cached.user_id == "U123"
    assert cached.roles == [Role.ENGINEER]

    # A brand-new command invocation (a fresh `citadel status ...` in the
    # design doc's own terms) reads the cache from disk, not from any
    # in-process state `login` left behind.
    result = runner.invoke(cli_main.app, ["status", "T-DOES-NOT-EXIST"])
    assert "no session cached" not in result.output
    assert "UNKNOWN_TASK" in result.output  # reached the API; session was read fine


def test_login_with_bad_password_is_rejected_cleanly(db, engineer):
    client = TestClient(create_app())
    _bind_client(client)

    result = runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "wrong-pw", "--base-url", "http://testserver"],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "UNAUTHENTICATED" in result.output
    assert not cli_session.config.SESSION_PATH.exists()


def test_admin_disable_tool_requires_admin_role(db, engineer):
    """A non-admin session gets one clear rejection line, not a stack trace
    -- this step's own explicit bar."""
    client = TestClient(create_app())
    _bind_client(client)

    login = runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", "http://testserver"],
    )
    assert login.exit_code == 0, login.output

    result = runner.invoke(cli_main.app, ["admin", "disable-tool", Tool.RAG_SEARCH])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "FORBIDDEN" in result.output
    assert "role" in result.output.lower()


def test_admin_disable_tool_succeeds_for_admin(db):
    create_user(
        db,
        username="s.mehta",
        password="admin-pw",
        roles=[Role.ADMIN],
        clearance=Classification.CONFIDENTIAL,
        department="security",
        user_id="U-ADMIN",
    )
    db.commit()

    client = TestClient(create_app())
    _bind_client(client)

    login = runner.invoke(
        cli_main.app,
        ["login", "--username", "s.mehta", "--password", "admin-pw", "--base-url", "http://testserver"],
    )
    assert login.exit_code == 0, login.output

    result = runner.invoke(cli_main.app, ["admin", "disable-tool", Tool.PYTHON_EXECUTE])
    assert result.exit_code == 0, result.output
    assert _field(result.output, "tool") == Tool.PYTHON_EXECUTE
    assert _field(result.output, "disabled") == "True"


def test_trace_renders_the_text_field(db, engineer):
    """`GET /tasks/{id}/trace`'s own `text` field (`app.observability.
    format_trace`) is what the CLI must print, verbatim -- including a
    TOOL_DENIED line reading as one more ordinary entry, not an error."""
    from app.db.models import Task

    task = Task(
        task_id="T-TRACE",
        user_id="U123",
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True},
    )
    db.add(task)
    db.commit()

    append_event("T-TRACE", "U123", EventType.TASK_CREATED, {"text": "trace rendering test"})
    append_event(
        "T-TRACE",
        "A123",
        EventType.TOOL_DENIED,
        {"tool": Tool.RAG_SEARCH, "code": "POLICY_DENIED", "reason": "acl disjoint from department"},
    )

    client = TestClient(create_app())
    _bind_client(client)

    login = runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", "http://testserver"],
    )
    assert login.exit_code == 0, login.output

    result = runner.invoke(cli_main.app, ["trace", "T-TRACE"])
    assert result.exit_code == 0, result.output
    assert "TASK_CREATED" in result.output
    assert "TOOL_DENIED" in result.output
    assert "not errors" in result.output  # the CLI's own legend line
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# A full task -> status -> approve -> trace run, driven entirely through the
# CLI's own commands, against fake rag.search/python.execute backends and a
# stubbed planner (no Ollama, no Docker) -- mirrors
# tests/test_artifact.py::test_generate_verify_approve_release_happy_path,
# but through `cli.main.app` instead of calling the HTTP API by hand.
# ---------------------------------------------------------------------------

_EVIDENCE_TEXT = "2026-06-14 SCHEDULED SERVICE\n2026-02-02 UNSCHEDULED REPAIR"

_DEFAULT_EVIDENCE: list[dict[str, Any]] = [
    {
        "evidence_id": "E001",
        "document_id": "DOC-P101-HIST",
        "document_version": "1",
        "page": 1,
        "text": _EVIDENCE_TEXT,
        "classification": Classification.CONFIDENTIAL,
        "acl": ["maintenance"],
        "provenance_id": "E001",
    }
]


def _good_plan(task_id: str) -> PlanModel:
    return PlanModel(
        plan_id="P000CLITEST",
        task_id=task_id,
        steps=[
            PlanStepModel(
                step_id="S1",
                agent_type="researcher",
                action=Tool.RAG_SEARCH,
                arguments={"query": "Pump P-101 maintenance history"},
            ),
            PlanStepModel(
                step_id="S2",
                agent_type="researcher",
                action=Tool.PYTHON_EXECUTE,
                arguments={"code": "from search_engine import search"},
            ),
            PlanStepModel(
                step_id="S3",
                agent_type="writer",
                action=Tool.GENERATE_REPORT,
                arguments={"template": "maintenance_summary_v1"},
            ),
        ],
    )


def _fake_python_execute(request: Any) -> dict[str, Any]:
    code = request.arguments["code"]
    assert "search_engine" not in code
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})
    return {"stdout": buf.getvalue(), "stderr": "", "exit_code": 0}


def _register_fake_backends(evidence: Optional[list[dict[str, Any]]] = None) -> None:
    rows = list(evidence) if evidence is not None else list(_DEFAULT_EVIDENCE)

    def _fake_rag_search(request: Any) -> dict[str, Any]:
        return {"results": rows}

    register_backend(Tool.RAG_SEARCH, _fake_rag_search)
    register_backend(Tool.PYTHON_EXECUTE, _fake_python_execute)
    register_report_backend()


def _stub_generate_plan(monkeypatch) -> None:
    import app.orchestrator.service as service_module

    def _fake(task_id: str, classification: str) -> PlanGenerationResult:
        return PlanGenerationResult(
            plan=_good_plan(task_id),
            model_id="qwen3-local",
            routing_reason="local_confidential_reasoning",
            repaired=False,
        )

    monkeypatch.setattr(service_module, "generate_plan", _fake)


def test_cli_happy_path_end_to_end(db, engineer, approver, monkeypatch):
    """section 1.1's whole script, typed as a human would: login, task,
    status, approve, trace -- ending in ARTIFACT_RELEASED / COMPLETED."""
    _register_fake_backends()
    _stub_generate_plan(monkeypatch)
    client = TestClient(create_app())
    _bind_client(client)

    login = runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", "http://testserver"],
    )
    assert login.exit_code == 0, login.output

    submit = runner.invoke(
        cli_main.app,
        [
            "task",
            (
                "Using the available internal maintenance documents, identify the "
                "recent maintenance history of Pump P-101 and generate a short "
                "maintenance summary report."
            ),
            "--classification",
            "CONFIDENTIAL",
        ],
    )
    assert submit.exit_code == 0, submit.output
    assert _field(submit.output, "status") == TaskStatus.WAITING_FOR_APPROVAL
    task_id = _field(submit.output, "task_id")
    artifact_id = _field(submit.output, "artifact_id")
    assert artifact_id and artifact_id != "None"

    status = runner.invoke(cli_main.app, ["status", task_id])
    assert status.exit_code == 0, status.output
    assert _field(status.output, "status") == TaskStatus.WAITING_FOR_APPROVAL

    # Switch identity -- a human approver logging in on the same shell.
    login_approver = runner.invoke(
        cli_main.app,
        ["login", "--username", "a.singh", "--password", "approver-pw", "--base-url", "http://testserver"],
    )
    assert login_approver.exit_code == 0, login_approver.output

    # `approve <task_id>` -- the task id, not the approval id, exercising
    # cli.client.CitadelClient.resolve_approval_id's own lookup.
    approve = runner.invoke(cli_main.app, ["approve", task_id, "--comment", "Looks correct."])
    assert approve.exit_code == 0, approve.output
    assert _field(approve.output, "decision") == "APPROVED"
    assert _field(approve.output, "task_status") == TaskStatus.COMPLETED
    assert _field(approve.output, "artifact_status") == "RELEASED"

    trace = runner.invoke(cli_main.app, ["trace", task_id])
    assert trace.exit_code == 0, trace.output
    assert "APPROVAL_GRANTED" in trace.output
    assert "ARTIFACT_RELEASED" in trace.output
    assert trace.output.index("APPROVAL_GRANTED") < trace.output.index("ARTIFACT_RELEASED")


def test_cli_reject_triggers_the_scoped_revision(db, engineer, approver, monkeypatch):
    """`citadel reject <id> --comment "..."` -- section 5.3's one bounded
    revision, triggered from the CLI, not just from a test calling the
    approval router directly."""
    _register_fake_backends()
    _stub_generate_plan(monkeypatch)
    client = TestClient(create_app())
    _bind_client(client)

    runner.invoke(
        cli_main.app,
        ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", "http://testserver"],
    )
    submit = runner.invoke(
        cli_main.app,
        [
            "task",
            "Identify Pump P-101's recent maintenance history and summarize it.",
            "--classification",
            "CONFIDENTIAL",
        ],
    )
    assert submit.exit_code == 0, submit.output
    task_id = _field(submit.output, "task_id")

    runner.invoke(
        cli_main.app,
        ["login", "--username", "a.singh", "--password", "approver-pw", "--base-url", "http://testserver"],
    )
    reject = runner.invoke(cli_main.app, ["reject", task_id, "--comment", "Add more detail."])
    assert reject.exit_code == 0, reject.output
    assert _field(reject.output, "decision") == "REJECTED"

    trace = runner.invoke(cli_main.app, ["trace", task_id])
    assert "APPROVAL_REJECTED" in trace.output


# ---------------------------------------------------------------------------
# Live, end-to-end, over a real socket -- skips cleanly if Ollama or Docker
# is unreachable (the same discipline every prior step's own test suite uses,
# per docs/BUILD_LOG.md).
# ---------------------------------------------------------------------------


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


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


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable at localhost:11434")
@pytest.mark.skipif(not _docker_reachable(), reason="Docker daemon is not reachable")
def test_live_task_end_to_end_over_a_real_socket(db, tmp_path, monkeypatch):
    """The real thing: a real uvicorn server, a real Execution Service
    subprocess, a real `hermes3` planning call, over a real HTTP socket --
    driven entirely through `cli.main.app` with its default (real) client,
    exactly as a human typing `citadel ...` in a shell would."""
    import subprocess
    import sys

    import httpx
    import uvicorn

    repo_root = Path(__file__).resolve().parent.parent

    exec_port = _free_port()
    env = os.environ.copy()
    env["CITADEL_EXECUTION_HOST"] = "127.0.0.1"
    env["CITADEL_EXECUTION_PORT"] = str(exec_port)
    exec_proc = subprocess.Popen(
        [sys.executable, "-m", "execution_service"],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    exec_url = f"http://127.0.0.1:{exec_port}"
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            if exec_proc.poll() is not None:
                pytest.fail(f"execution_service exited early:\n{exec_proc.stdout.read()}")
            try:
                if httpx.get(f"{exec_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            pytest.fail("execution_service never became healthy")

        monkeypatch.setenv("CITADEL_EXECUTION_SERVICE_URL", exec_url)

        create_user(
            db,
            username="j.rao",
            password="engineer-pw",
            roles=[Role.ENGINEER],
            clearance=Classification.CONFIDENTIAL,
            department="maintenance",
            user_id="U123",
        )
        create_user(
            db,
            username="a.singh",
            password="approver-pw",
            roles=[Role.APPROVER],
            clearance=Classification.CONFIDENTIAL,
            department="maintenance",
            user_id="U-APPROVER",
        )
        db.commit()

        app_port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(create_app(), host="127.0.0.1", port=app_port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{app_port}"
        deadline = time.time() + 30
        while time.time() < deadline and not getattr(server, "started", False):
            time.sleep(0.25)
        assert getattr(server, "started", False), "app server never started"

        try:
            login = runner.invoke(
                cli_main.app,
                ["login", "--username", "j.rao", "--password", "engineer-pw", "--base-url", base_url],
            )
            assert login.exit_code == 0, login.output

            submit = runner.invoke(
                cli_main.app,
                [
                    "task",
                    (
                        "Using the available internal maintenance documents, identify the "
                        "recent maintenance history of Pump P-101 and generate a short "
                        "maintenance summary report."
                    ),
                    "--classification",
                    "CONFIDENTIAL",
                ],
            )
            assert submit.exit_code == 0, submit.output
            task_id = _field(submit.output, "task_id")
            task_status = _field(submit.output, "status")

            trace = runner.invoke(cli_main.app, ["trace", task_id])
            assert trace.exit_code == 0, trace.output

            if task_status == TaskStatus.WAITING_FOR_APPROVAL:
                runner.invoke(
                    cli_main.app,
                    ["login", "--username", "a.singh", "--password", "approver-pw", "--base-url", base_url],
                )
                approve = runner.invoke(cli_main.app, ["approve", task_id])
                assert approve.exit_code == 0, approve.output
                assert _field(approve.output, "task_status") == TaskStatus.COMPLETED
            else:
                # A live model call is not perfectly deterministic (docs/
                # BUILD_LOG.md's own note); as long as the CLI drove the real
                # HTTP surface end to end and the trace is readable, the CLI
                # layer this step owns is proven regardless of the model's
                # own plan quality on this particular run.
                assert task_status == TaskStatus.FAILED
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    finally:
        exec_proc.terminate()
        try:
            exec_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exec_proc.kill()
