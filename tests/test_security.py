"""Design doc §9 -- Security.

The five checklist lines owned by `security-control-plane` (step 4) are
implemented here against the fake `echo` tool, driven through the *whole* Tool
Gateway path (§6.6 Step A -> Step B -> Step C). That order is the point of
§8's step 4 and of C-004: ALLOW and DENY are proven before any real tool
exists, so steps 5-8 inherit a spine that is already known to be correct.

    [x] Authorized tool call -> ALLOW
    [x] Unauthorized classification -> DENY
    [x] Unauthorized ACL/department -> DENY (the denial-path demo, §1.2)
    [x] Expired capability -> DENY
    [x] Disabled tool -> DENY even with a valid capability (§1.3)
    [ ] No code path exists for the execution zone to reach Postgres directly
    [ ] No code path exists for the execution zone to reach the Docker socket

The last two belong to `execution-service` (step 5) and are skipped
individually below.

Everything after the five is step 4 proving its own contract: Identity (§6.4),
capability signing and expiry (§6.5), the ordered rule chain (§6.7), the
emergency control (§6.8), and the invariant that no tool is reachable except
through the gateway.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.capability import (
    CapabilityExpired,
    CapabilityInvalid,
    CapabilityIssueError,
    CapabilityScope,
    issue_capability,
    issue_for_step,
    verify_capability,
)
from app.db.models import Agent, Task
from app.db.state_machines import Classification, Role
from app.identity import (
    AuthenticationFailed,
    SessionInvalid,
    authenticate,
    create_user,
    hash_password,
    issue_session_token,
    verify_password,
    verify_session_token,
)
from app.main import create_app
from app.observability import EventType, get_trace, verify_chain
from app.policy import (
    Decision,
    PolicyAction,
    PolicyAgent,
    PolicyResource,
    PolicyTask,
    PolicyUser,
    Rule,
    Tool,
    decide,
    evaluate,
    get_registry,
)
from app.tool_gateway import (
    ENVELOPE_KEYS,
    ErrorCode,
    clear_backends,
    invoke,
    register_backend,
    task_resource,
)
from app.tool_gateway.backends.echo import echo

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

#: The demo's authorized target (§1.1 step 9): maintenance documents, at the
#: task's own classification, ACL'd to the task's department.
MAINTENANCE_DOC = PolicyResource.build(
    "DOC-P101-HIST", "document", Classification.CONFIDENTIAL, ["maintenance", "engineering"]
)

#: The denial-path target (§1.2): "a query whose target documents are tagged
#: acl: ["finance"], outside the task's granted department: "maintenance"".
FINANCE_DOC = PolicyResource.build(
    "DOC-Q3-FIN", "document", Classification.CONFIDENTIAL, ["finance"]
)


@pytest.fixture(autouse=True)
def clean_control_plane_state():
    """`tool_disabled` and the backend registry are process-global by design
    (§6.8 is one flag dict; §6.6 is one routing table). Reset both around every
    test so one test's emergency control cannot leak into another's."""
    get_registry().clear()
    clear_backends()
    yield
    get_registry().clear()
    clear_backends()


@pytest.fixture
def agent(db, task):
    """The one Agent per task (§3, BB-014)."""
    row = Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def echo_backend():
    """Bind the fake echo tool to `rag.search`.

    §6.7 ALLOWs only the three real tool names, so the ALLOW path has to be
    exercised under one of them -- which is exactly what §8's step 4 asks for:
    the pipeline is proven with a backend that has no behaviour of its own,
    and step 6 swaps in the real one with another `register_backend` call.
    """
    register_backend(Tool.RAG_SEARCH, echo)
    return echo


@pytest.fixture
def internal_task(db, engineer):
    """A task classified below the resource it will reach for -- the input to
    §6.7's `resource.classification > task.classification` rule. The lattice
    is PUBLIC < INTERNAL < CONFIDENTIAL (§3/§4)."""
    row = Task(
        task_id="T-INTERNAL",
        user_id=engineer.user_id,
        classification=Classification.INTERNAL,
        requirements={"needs_rag": True},
    )
    db.add(row)
    db.flush()
    db.add(Agent(agent_id="A-INTERNAL", task_id=row.task_id, agent_type="researcher"))
    db.commit()
    return row


@pytest.fixture
def api(db):
    """TestClient over the trusted-zone FastAPI app (§2)."""
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def admin(db):
    user = create_user(
        db,
        username="s.mehta",
        password="admin-pw",
        roles=[Role.ADMIN],
        clearance=Classification.CONFIDENTIAL,
        department="security",
        user_id="U-ADMIN",
    )
    db.commit()
    return user


@pytest.fixture
def engineer_with_password(db, engineer):
    engineer.password_hash = hash_password("engineer-pw")
    db.commit()
    return engineer


def event_types(task_id):
    return [event.event_type for event in get_trace(task_id)]


def last_event(task_id, event_type):
    matches = [e for e in get_trace(task_id) if e.event_type == event_type]
    assert matches, f"no {event_type} event was recorded for {task_id}"
    return matches[-1]


class _SpyBackend:
    """Records whether the gateway ever reached Step C."""

    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return {"reached": True}


# ==========================================================================
# §9 Security -- the five lines owned by step 4
# ==========================================================================


def test_authorized_tool_call_is_allowed(db, task, agent, echo_backend):
    """Authorized tool call -> ALLOW"""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,
        arguments={"query": "Pump P-101 maintenance history"},
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["tool"] == Tool.RAG_SEARCH
    assert result["metadata"]["execution_id"].startswith("EXEC")
    # The fake tool really ran, and it ran with the verified capability.
    assert result["result"]["echo"] == {"query": "Pump P-101 maintenance history"}
    assert result["result"]["capability_id"] == capability.capability_id
    assert result["result"]["requester"] == {
        "task_id": task.task_id,
        "agent_id": agent.agent_id,
        "classification_max": Classification.CONFIDENTIAL,
        "department": "maintenance",
    }

    # Both checks happened, in order, and exactly once each.
    assert event_types(task.task_id) == [
        EventType.CAPABILITY_CHECKED,
        EventType.POLICY_DECISION,
        EventType.TOOL_EXECUTED,
    ]
    decision = last_event(task.task_id, EventType.POLICY_DECISION)
    assert decision.payload["decision"] == Decision.ALLOW
    assert decision.payload["rule"] == Rule.TOOL_ALLOWED
    assert verify_chain(task.task_id) is True


def test_unauthorized_classification_is_denied(db, internal_task, echo_backend):
    """Unauthorized classification -> DENY"""
    spy = _SpyBackend()
    register_backend(Tool.RAG_SEARCH, spy)

    capability = issue_for_step(
        internal_task.task_id, "A-INTERNAL", Tool.RAG_SEARCH
    )

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,  # CONFIDENTIAL > the task's INTERNAL
        arguments={"query": "Pump P-101 maintenance history"},
    )

    assert result["success"] is False
    assert result["result"] is None
    assert result["error"]["code"] == ErrorCode.POLICY_DENIED
    assert "exceeds task classification" in result["error"]["message"]
    assert spy.calls == [], "the backend must never be reached on a DENY"

    assert event_types(internal_task.task_id) == [
        EventType.CAPABILITY_CHECKED,
        EventType.POLICY_DECISION,
        EventType.TOOL_DENIED,
    ]
    denial = last_event(internal_task.task_id, EventType.TOOL_DENIED)
    assert denial.payload["rule"] == Rule.CLASSIFICATION_EXCEEDS_TASK
    assert denial.payload["resource"]["resource_id"] == "DOC-P101-HIST"


def test_unauthorized_acl_or_department_is_denied(db, task, agent):
    """Unauthorized ACL/department -> DENY (the denial-path demo, §1.2)

    §1.2 walked literally: the capability scope check passes (the operation is
    still rag.search), the Policy Engine finds resource.acl disjoint from the
    task's department, the tool is NOT executed, and TOOL_DENIED is recorded
    with the requested resource and the reason.
    """
    spy = _SpyBackend()
    register_backend(Tool.RAG_SEARCH, spy)

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=FINANCE_DOC,
        arguments={"query": "Q3 finance report"},
    )

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.POLICY_DENIED
    assert spy.calls == [], "no data may leave the Data Plane on a denial"

    # The capability check passed -- that is what makes this a *policy* denial
    # and not a capability failure.
    capability_check = last_event(task.task_id, EventType.CAPABILITY_CHECKED)
    assert capability_check.payload["valid"] is True

    denial = last_event(task.task_id, EventType.TOOL_DENIED)
    assert denial.payload["rule"] == Rule.ACL_DISJOINT_FROM_DEPARTMENT
    assert denial.payload["resource"]["acl"] == ["finance"]
    assert denial.payload["arguments"] == {"query": "Q3 finance report"}
    assert denial.task_id == task.task_id
    assert "disjoint" in denial.payload["reason"]


def test_expired_capability_is_denied(db, task, agent):
    """Expired capability -> DENY"""
    spy = _SpyBackend()
    register_backend(Tool.RAG_SEARCH, spy)

    # §6.5 gives capabilities a 5-minute TTL and no revocation list; a negative
    # TTL is how the suite produces an aged-out token without waiting.
    capability = issue_for_step(
        task.task_id, agent.agent_id, Tool.RAG_SEARCH, ttl_seconds=-1
    )

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,
        arguments={"query": "Pump P-101 maintenance history"},
    )

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.CAPABILITY_EXPIRED
    assert spy.calls == []

    # Step A failed, so Step B was never consulted: no POLICY_DECISION exists.
    types = event_types(task.task_id)
    assert types == [EventType.CAPABILITY_CHECKED, EventType.TOOL_DENIED]
    check = last_event(task.task_id, EventType.CAPABILITY_CHECKED)
    assert check.payload["valid"] is False
    assert check.payload["code"] == ErrorCode.CAPABILITY_EXPIRED
    assert last_event(task.task_id, EventType.TOOL_DENIED).payload["stage"] == "capability"


def test_disabled_tool_is_denied_even_with_a_valid_capability(db, task, agent):
    """Disabled tool -> DENY even with a valid capability (§1.3)

    The capability is minted first and is still perfectly valid when the call
    is made -- there is no revocation list to put it on (§6.5). Only the policy
    layer changes, and policy is consulted on every call.
    """
    spy = _SpyBackend()
    register_backend(Tool.RAG_SEARCH, spy)

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    # The token itself is unaffected by the emergency control:
    assert verify_capability(capability.token, operation=Tool.RAG_SEARCH)

    get_registry().disable(Tool.RAG_SEARCH)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,
        arguments={"query": "Pump P-101 maintenance history"},
    )

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.TOOL_DISABLED
    assert spy.calls == []

    denial = last_event(task.task_id, EventType.TOOL_DENIED)
    assert denial.payload["rule"] == Rule.TOOL_DISABLED
    assert denial.payload["reason"] == (
        f"tool '{Tool.RAG_SEARCH}' disabled by administrator"
    )
    # Step A still passed. The capability was never the thing that stopped it.
    assert last_event(task.task_id, EventType.CAPABILITY_CHECKED).payload["valid"] is True


# ==========================================================================
# §9 Security -- owned by execution-service (step 5)
# ==========================================================================
#
# Both lines below are structural checks in the spirit of
# `test_no_code_path_invokes_a_tool_outside_the_gateway` above: a code path
# either exists in the source tree or it doesn't, so "no code path exists" is
# provable by walking the tree rather than by exercising the running system.
# Each has a negative control proving the detector actually catches a planted
# violation, the same discipline `test_the_tool_name_detector_is_not_vacuous`
# applies to the tool-name check.

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: design doc §2: the ONE thing every reference to a Docker socket looks
#: like, whichever transport a platform uses to reach the daemon.
_DOCKER_SOCKET_MARKERS = (
    "/var/run/docker.sock",
    "docker_engine",  # the Windows named-pipe form, \\.\pipe\docker_engine
)

#: The Postgres/SQLAlchemy driver names, plus `app` itself -- the one
#: SQLAlchemy engine and Postgres connection this system has lives in
#: `app/db/engine.py` (design doc §6.11: "the Orchestrator process is the
#: only holder of the Data Plane's write credential"), so a module that
#: cannot import `app` at all cannot reach it either.
_POSTGRES_CODE_PATH_ROOTS = frozenset({"sqlalchemy", "psycopg", "psycopg2", "asyncpg", "app"})


def _imported_module_roots(source: str) -> set[str]:
    """Top-level package names named by any `import`/`from ... import` in
    `source` -- `import sqlalchemy.orm` and `from sqlalchemy import x` both
    yield `{"sqlalchemy"}`. Relative imports (`from .foo import x`) are
    excluded deliberately: they stay inside the same package, which is
    walked in full anyway."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def _non_docstring_string_literals(source: str) -> list[tuple[int, str]]:
    """Every string-constant literal in `source`, excluding a module's (or a
    class's/function's) own opening docstring -- matches
    `_tool_name_literals`'s exemption above, so prose explaining *why* a
    boundary exists does not itself trip the boundary's own test."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        found.append((node.lineno, node.value))
    return found


def test_execution_zone_has_no_code_path_to_postgres():
    """No code path exists for the execution zone to reach Postgres directly

    Walked as design doc §6.11 states the guarantee: enforced at the network
    level (no route from the execution zone to Postgres -- proven for the
    internet case by `tests/test_execution.py`'s §6.13 test, the same Docker
    network property) *and* at the application level, independent of the
    network. This is the application-level half: `execution_service/` -- the
    isolated zone's own code -- imports neither a Postgres driver nor
    `app` (where the system's one database engine lives), so there is
    nothing in that process capable of opening a Postgres connection even if
    a network route existed.
    """
    offenders = []
    for path in (_REPO_ROOT / "execution_service").rglob("*.py"):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        hit = _imported_module_roots(path.read_text(encoding="utf-8")) & _POSTGRES_CODE_PATH_ROOTS
        if hit:
            offenders.append(f"{rel} imports {sorted(hit)}")

    assert offenders == [], (
        "the execution zone has a code path to Postgres: " + "; ".join(offenders) +
        ". execution_service/ must never import a Postgres driver, SQLAlchemy, "
        "or app/ (where the one SQLAlchemy engine in this system lives)."
    )


def test_the_postgres_code_path_detector_is_not_vacuous():
    """Negative control: prove the detector catches a planted violation
    rather than passing because it checks nothing."""
    assert "sqlalchemy" in _imported_module_roots("import sqlalchemy\n")
    assert "app" in _imported_module_roots("from app.db.engine import SessionLocal\n")
    assert _imported_module_roots("from . import settings\n") == set()
    assert _imported_module_roots("import fastapi\nimport docker\n").isdisjoint(
        _POSTGRES_CODE_PATH_ROOTS
    )


def test_execution_zone_has_no_code_path_to_the_docker_socket():
    """No code path exists for the execution zone to reach the Docker socket

    Read literally per BB-016 (§2, §6.6): "Execution Service is the only
    Docker-socket holder; agent loop itself never runs untrusted code" --
    the socket access that must not exist is a path FROM the trusted
    workflow zone (`app/`, where the agent loop and every trusted component
    run) TO the Docker socket. `execution_service/` importing `docker` is
    the intended design, not a violation; the violation this test rules out
    is the same capability appearing anywhere under `app/`.
    """
    offenders = []
    for path in (_REPO_ROOT / "app").rglob("*.py"):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")

        if "docker" in _imported_module_roots(source):
            offenders.append(f"{rel} imports the docker package")

        for lineno, literal in _non_docstring_string_literals(source):
            for marker in _DOCKER_SOCKET_MARKERS:
                if marker in literal:
                    offenders.append(f"{rel}:{lineno} references {marker!r}")

    assert offenders == [], (
        "the trusted workflow zone has a code path to the Docker socket: " +
        "; ".join(offenders) + ". Only execution_service/ may import the "
        "docker package or reference a Docker socket path; app/ talks to it "
        "over HTTP only, via app/execution/backend.py."
    )


def test_the_docker_socket_detector_is_not_vacuous():
    """Negative control for the test above."""
    assert "docker" in _imported_module_roots("import docker\n")
    assert "docker" in _imported_module_roots("from docker import DockerClient\n")
    caught = _non_docstring_string_literals('x = "/var/run/docker.sock"\n')
    assert caught == [(1, "/var/run/docker.sock")]
    assert _non_docstring_string_literals('"""mentions /var/run/docker.sock"""\n') == []


# ==========================================================================
# The Tool Gateway's own contract (§6.6)
# ==========================================================================


def test_the_envelope_is_the_same_shape_however_the_call_failed(db, task, agent):
    """BB-017: "one uniform result envelope for every tool regardless of
    backend" -- and, per §6.6, regardless of whether capability, policy or
    execution was what failed."""
    register_backend(Tool.RAG_SEARCH, echo)
    good = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    expired = issue_for_step(
        task.task_id, agent.agent_id, Tool.RAG_SEARCH, ttl_seconds=-1
    )

    envelopes = [
        invoke(  # success
            capability_token=good.token, tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC, arguments={"q": 1},
        ),
        invoke(  # capability failure
            capability_token=expired.token, tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC, arguments={"q": 1},
        ),
        invoke(  # policy failure
            capability_token=good.token, tool=Tool.RAG_SEARCH,
            resource=FINANCE_DOC, arguments={"q": 1},
        ),
        invoke(  # execution failure
            capability_token=good.token, tool=Tool.RAG_SEARCH,
            resource=MAINTENANCE_DOC, arguments={"fail": True},
        ),
    ]

    for result in envelopes:
        assert set(result) == ENVELOPE_KEYS
        assert isinstance(result["success"], bool)
        assert result["tool"] == Tool.RAG_SEARCH
        assert result["metadata"]["execution_id"].startswith("EXEC")
        if result["success"]:
            assert result["error"] is None and result["result"] is not None
        else:
            assert result["result"] is None
            assert set(result["error"]) == {"code", "message"}

    assert [e["error"] and e["error"]["code"] for e in envelopes] == [
        None,
        ErrorCode.CAPABILITY_EXPIRED,
        ErrorCode.POLICY_DENIED,
        ErrorCode.EXECUTION_ERROR,
    ]


def test_an_execution_failure_is_not_recorded_as_a_denial(db, task, agent, echo_backend):
    """Authorization succeeded; only the backend failed. Mislabelling that as
    TOOL_DENIED would corrupt the one signal §1.2 and §1.3 rest on."""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,
        arguments={"fail": True, "fail_message": "backend exploded"},
    )

    assert result["error"]["code"] == ErrorCode.EXECUTION_ERROR
    assert "backend exploded" in result["error"]["message"]
    types = event_types(task.task_id)
    assert EventType.TOOL_DENIED not in types
    executed = last_event(task.task_id, EventType.TOOL_EXECUTED)
    assert executed.payload["success"] is False
    assert executed.payload["error"]["code"] == ErrorCode.EXECUTION_ERROR


def test_an_unregistered_tool_fails_rather_than_finding_a_stand_in(db, task, agent):
    """Nothing is pre-registered, so a backend that fails to load is an
    EXECUTION_ERROR -- never a fake that quietly answers in its place."""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC,
    )

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.EXECUTION_ERROR
    assert "no backend is registered" in result["error"]["message"]


def test_capability_and_policy_are_two_separate_checks(db, task, agent, echo_backend):
    """C-002: the capability proves "this agent may attempt rag.search in
    general"; policy decides "is THIS document allowed right now". One token,
    two different answers, depending only on the resource."""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    allowed = invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC, arguments={"q": "pump"},
    )
    denied = invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=FINANCE_DOC, arguments={"q": "finance"},
    )

    assert allowed["success"] is True
    assert denied["success"] is False
    assert denied["error"]["code"] == ErrorCode.POLICY_DENIED


def test_policy_is_consulted_exactly_once_per_call(db, task, agent, echo_backend):
    """§6.6 Step B: "calls Control Plane's Policy Engine ONCE per call"."""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC, arguments={"q": "pump"},
    )
    decisions = [
        e for e in get_trace(task.task_id) if e.event_type == EventType.POLICY_DECISION
    ]
    assert len(decisions) == 1


def test_the_acting_task_comes_from_the_capability_not_the_caller(
    db, task, agent, internal_task, echo_backend
):
    """The gateway takes task_id/agent_id from the verified token and looks
    everything else up from there -- there is no argument a caller could use to
    act on another task with a good capability (§6.4's rule, applied to the
    gateway)."""
    capability = issue_for_step(
        internal_task.task_id, "A-INTERNAL", Tool.RAG_SEARCH
    )
    result = invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=PolicyResource.build(
            "DOC-X", "document", Classification.INTERNAL, ["maintenance"]
        ),
    )
    assert result["success"] is True
    # Recorded against T-INTERNAL, the task the *token* named.
    assert event_types(internal_task.task_id) != []
    assert get_trace(task.task_id) == []


def test_a_denial_is_committed_independently_of_the_caller(db, task, agent):
    """A denial is a first-class outcome (§1.2), so it must be durable the
    moment it happens rather than riding on whatever transaction the caller
    later decides to abandon."""
    register_backend(Tool.RAG_SEARCH, echo)
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)

    invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=FINANCE_DOC, arguments={"q": "finance"},
    )
    db.rollback()  # the caller abandons its work

    assert EventType.TOOL_DENIED in event_types(task.task_id)


def test_task_resource_helper_still_runs_both_data_rules(db, task, agent, echo_backend):
    """A tool with no external target is checked against the task's own
    envelope -- it is not waved through."""
    resource = task_resource(task.task_id)
    assert resource.classification == Classification.CONFIDENTIAL
    assert resource.acl == ("maintenance",)

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    result = invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH, resource=resource
    )
    assert result["success"] is True


# ==========================================================================
# Policy Engine -- §6.7's ordered chain
# ==========================================================================


def _inputs(tool, resource, *, task_classification=Classification.CONFIDENTIAL):
    user = PolicyUser("U123", ("engineer",), Classification.CONFIDENTIAL, "maintenance")
    agent = PolicyAgent("A123", "T123")
    task = PolicyTask("T123", task_classification, "maintenance")
    return user, agent, task, PolicyAction(tool), resource


def test_decide_returns_the_three_documented_values_only():
    assert decide(*_inputs(Tool.RAG_SEARCH, MAINTENANCE_DOC)) == Decision.ALLOW
    assert decide(*_inputs(Tool.HOST_SHELL, MAINTENANCE_DOC)) == Decision.DENY
    assert (
        decide(*_inputs(Tool.ARTIFACT_RELEASE, MAINTENANCE_DOC))
        == Decision.REQUIRE_APPROVAL
    )


def test_disabled_beats_every_other_rule_including_the_allow_list():
    """The `tool_disabled` check is first, so no arrangement of scope,
    clearance or ACL can get past it."""
    get_registry().disable(Tool.RAG_SEARCH)
    outcome = evaluate(*_inputs(Tool.RAG_SEARCH, MAINTENANCE_DOC))
    assert outcome.decision == Decision.DENY
    assert outcome.rule == Rule.TOOL_DISABLED


def test_host_shell_is_denied_before_any_data_rule_can_be_reached():
    outcome = evaluate(*_inputs(Tool.HOST_SHELL, MAINTENANCE_DOC))
    assert (outcome.decision, outcome.rule) == (Decision.DENY, Rule.HOST_SHELL_FORBIDDEN)


def test_artifact_release_is_routed_to_a_human_before_the_data_rules():
    """Third in the chain, so it is never quietly ALLOWed by a permissive
    resource -- and never silently DENIED by a restrictive one either."""
    outcome = evaluate(*_inputs(Tool.ARTIFACT_RELEASE, FINANCE_DOC))
    assert outcome.decision == Decision.REQUIRE_APPROVAL
    assert outcome.rule == Rule.ARTIFACT_RELEASE_NEEDS_APPROVAL


def test_an_allowed_tool_is_still_subject_to_the_two_data_rules():
    """The allow list is second-to-last for a reason: being an allowed tool is
    never sufficient on its own."""
    assert evaluate(*_inputs(Tool.RAG_SEARCH, FINANCE_DOC)).rule == (
        Rule.ACL_DISJOINT_FROM_DEPARTMENT
    )
    assert evaluate(
        *_inputs(
            Tool.RAG_SEARCH, MAINTENANCE_DOC, task_classification=Classification.INTERNAL
        )
    ).rule == Rule.CLASSIFICATION_EXCEEDS_TASK


def test_an_unmatched_tool_is_denied_by_default(db, task, agent):
    """§6.7's tail: "fail closed -- anything not explicitly matched is
    denied". The fake echo tool under its own name is exactly such a tool."""
    outcome = evaluate(*_inputs(Tool.ECHO, MAINTENANCE_DOC))
    assert (outcome.decision, outcome.rule) == (Decision.DENY, Rule.DEFAULT_DENY)

    # ... and the same through the whole gateway, with the backend registered.
    register_backend(Tool.ECHO, echo)
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.ECHO)
    result = invoke(
        capability_token=capability.token, tool=Tool.ECHO, resource=MAINTENANCE_DOC
    )
    assert result["error"]["code"] == ErrorCode.POLICY_DENIED
    assert last_event(task.task_id, EventType.TOOL_DENIED).payload["rule"] == (
        Rule.DEFAULT_DENY
    )


def test_classification_uses_the_lattice_not_string_comparison():
    """Under Python string ordering "PUBLIC" > "INTERNAL", which would deny a
    PUBLIC document to an INTERNAL task. The §3/§4 lattice is the arbiter."""
    public_doc = PolicyResource.build(
        "DOC-PUB", "document", Classification.PUBLIC, ["maintenance"]
    )
    outcome = evaluate(
        *_inputs(Tool.RAG_SEARCH, public_doc, task_classification=Classification.INTERNAL)
    )
    assert outcome.decision == Decision.ALLOW


def test_an_unknown_classification_marking_fails_closed():
    weird = PolicyResource.build("DOC-?", "document", "COSMIC_TOP_SECRET", ["maintenance"])
    outcome = evaluate(*_inputs(Tool.RAG_SEARCH, weird))
    assert (outcome.decision, outcome.rule) == (
        Decision.DENY,
        Rule.UNKNOWN_CLASSIFICATION,
    )


def test_decide_and_evaluate_can_never_disagree():
    """`evaluate` is the chain; `decide` is a projection of it."""
    resources = [MAINTENANCE_DOC, FINANCE_DOC]
    tools = [
        Tool.RAG_SEARCH, Tool.PYTHON_EXECUTE, Tool.GENERATE_REPORT,
        Tool.HOST_SHELL, Tool.ARTIFACT_RELEASE, Tool.ECHO,
    ]
    for tool in tools:
        for resource in resources:
            args = _inputs(tool, resource)
            assert decide(*args) == evaluate(*args).decision


def test_the_policy_engine_is_pure(db, task):
    """It emits no events and opens no session -- the Tool Gateway is the one
    place a POLICY_DECISION is recorded, so a decision can neither be logged
    twice nor logged without having been made."""
    evaluate(*_inputs(Tool.RAG_SEARCH, MAINTENANCE_DOC))
    assert get_trace(None) == []


# ==========================================================================
# Emergency control -- §6.8
# ==========================================================================


def test_only_disable_tool_exists_as_an_emergency_control():
    """§6.8: "KILL TASK, KILL AGENT, DISABLE MODEL, GLOBAL NETWORK BLOCK,
    QUARANTINE ARTIFACT are explicitly not built for this slice"."""
    from app.policy import tool_disabled as module

    surface = {name for name in dir(module) if not name.startswith("_")}
    forbidden = {"kill_task", "kill_agent", "disable_model", "network_block", "quarantine"}
    assert surface.isdisjoint(forbidden)

    # Enumerate via the OpenAPI schema rather than `app.routes`: `include_router`
    # yields `_IncludedRouter` wrappers that expose no `.path`, so walking
    # `.routes` silently misses every mounted endpoint.
    routes = set(create_app().openapi()["paths"])
    admin_routes = {path for path in routes if path.startswith("/admin")}
    assert admin_routes == {"/admin/tools/{tool_name}/disable"}


def test_admin_can_disable_a_tool_and_it_takes_effect_immediately(
    db, api, admin, task, agent, echo_backend
):
    """§1.3, end to end over HTTP: an admin disables the tool, and a capability
    minted *before* the disable stops working on the very next call."""
    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    token, _ = issue_session_token(admin.user_id, admin.roles)

    before = invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC, arguments={"q": "pump"},
    )
    assert before["success"] is True

    response = api.post(
        f"/admin/tools/{Tool.RAG_SEARCH}/disable",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "tool": Tool.RAG_SEARCH,
        "disabled": True,
        "previously_disabled": False,
        "known_tool": True,
    }

    after = invoke(
        capability_token=capability.token, tool=Tool.RAG_SEARCH,
        resource=MAINTENANCE_DOC, arguments={"q": "pump"},
    )
    assert after["error"]["code"] == ErrorCode.TOOL_DISABLED

    # The admin action itself is on the chain, system-scoped (no task_id).
    system_events = [e for e in get_trace(None) if e.task_id is None]
    assert [e.payload["control"] for e in system_events] == ["DISABLE_TOOL"]
    assert system_events[0].actor_id == admin.user_id
    assert verify_chain() is True


def test_disable_tool_is_admin_only(db, api, engineer_with_password, task):
    token, _ = issue_session_token(
        engineer_with_password.user_id, engineer_with_password.roles
    )
    response = api.post(
        f"/admin/tools/{Tool.RAG_SEARCH}/disable",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert get_registry().get(Tool.RAG_SEARCH) is False


def test_disable_tool_requires_a_session_at_all(db, api):
    assert api.post(f"/admin/tools/{Tool.RAG_SEARCH}/disable").status_code == 401


# ==========================================================================
# Identity -- §6.4
# ==========================================================================


def test_login_issues_an_eight_hour_session_jwt(db, api, engineer_with_password):
    response = api.post(
        "/login", json={"username": "j.rao", "password": "engineer-pw"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user_id"] == engineer_with_password.user_id
    assert body["roles"] == [Role.ENGINEER]
    assert "password" not in response.text and "hash" not in response.text

    identity = verify_session_token(body["access_token"])
    assert identity.user_id == engineer_with_password.user_id
    assert identity.roles == (Role.ENGINEER,)
    ttl = identity.expires_at - identity.issued_at
    assert ttl.total_seconds() == config.SESSION_TTL_HOURS * 3600 == 8 * 3600


def test_login_rejects_a_bad_password_without_revealing_which_part_was_wrong(
    db, api, engineer_with_password
):
    wrong_password = api.post(
        "/login", json={"username": "j.rao", "password": "not-it"}
    )
    unknown_user = api.post(
        "/login", json={"username": "nobody", "password": "not-it"}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_a_user_with_no_password_hash_can_never_authenticate(db, engineer):
    """The column is nullable; absent credentials are not blank credentials."""
    assert engineer.password_hash is None
    with pytest.raises(AuthenticationFailed):
        authenticate(db, "j.rao", "")
    assert verify_password("", None) is False


def test_bcrypt_hashes_are_salted_and_verifiable():
    first = hash_password("engineer-pw")
    second = hash_password("engineer-pw")
    assert first != "engineer-pw" and first != second  # salted
    assert verify_password("engineer-pw", first)
    assert not verify_password("engineer-pw ", first)


def test_an_expired_session_token_is_refused(db, api, engineer_with_password):
    token, _ = issue_session_token(
        engineer_with_password.user_id, engineer_with_password.roles, ttl_hours=-1
    )
    with pytest.raises(SessionInvalid):
        verify_session_token(token)
    response = api.post(
        f"/admin/tools/{Tool.RAG_SEARCH}/disable",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_a_tampered_session_token_is_refused(db, admin):
    token, _ = issue_session_token(admin.user_id, admin.roles)
    header, payload, signature = token.split(".")
    with pytest.raises(SessionInvalid):
        verify_session_token(f"{header}.{payload}.{signature[:-2]}xx")


def test_a_client_supplied_user_id_in_a_body_is_never_trusted(
    db, api, admin, engineer_with_password
):
    """§6.4's non-negotiable rule. An engineer who claims to be the admin in
    the request body is still an engineer, and an admin who claims to be
    someone else is still recorded as themselves."""
    engineer_token, _ = issue_session_token(
        engineer_with_password.user_id, engineer_with_password.roles
    )
    spoofed = api.post(
        f"/admin/tools/{Tool.RAG_SEARCH}/disable",
        headers={"Authorization": f"Bearer {engineer_token}"},
        json={
            "user_id": admin.user_id,
            "approver_id": admin.user_id,
            "roles": [Role.ADMIN],
        },
    )
    assert spoofed.status_code == 403
    assert get_registry().get(Tool.RAG_SEARCH) is False

    admin_token, _ = issue_session_token(admin.user_id, admin.roles)
    accepted = api.post(
        f"/admin/tools/{Tool.RAG_SEARCH}/disable",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": engineer_with_password.user_id},
    )
    assert accepted.status_code == 200
    system_event = [e for e in get_trace(None) if e.task_id is None][-1]
    assert system_event.actor_id == admin.user_id


def test_the_identity_fields_a_body_may_not_assert_include_approver_id():
    """§6.10's approver_id is called out by name in §6.4, so step 8 inherits
    the same discard list rather than writing its own."""
    from app.identity import CLIENT_IDENTITY_FIELDS, drop_client_identity

    assert {"user_id", "approver_id", "roles"} <= CLIENT_IDENTITY_FIELDS
    assert drop_client_identity(
        {"user_id": "U-evil", "approver_id": "U-evil", "decision": "APPROVED"}
    ) == {"decision": "APPROVED"}


# ==========================================================================
# Capability issuance and verification -- §6.5
# ==========================================================================


def test_a_capability_is_scoped_to_one_operation_and_one_task_agent_pair(
    db, task, agent
):
    issued = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    capability = verify_capability(
        issued.token,
        operation=Tool.RAG_SEARCH,
        task_id=task.task_id,
        agent_id=agent.agent_id,
    )
    assert capability.capability_id.startswith("CAP")
    assert capability.operation == Tool.RAG_SEARCH
    # Scope is derived from stored state, not from the caller (§6.5's example).
    assert capability.scope == CapabilityScope(
        classification_max=Classification.CONFIDENTIAL, department="maintenance"
    )
    assert capability.as_dict()["scope"] == {
        "classification_max": Classification.CONFIDENTIAL,
        "department": "maintenance",
    }


def test_the_default_capability_ttl_is_five_minutes(db, task, agent):
    assert config.CAPABILITY_TTL_SECONDS == 300
    issued = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    ttl = issued.capability.expires_at - issued.capability.issued_at
    assert ttl.total_seconds() == 300


def test_a_capability_for_one_operation_does_not_authorize_another(db, task, agent):
    issued = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    with pytest.raises(CapabilityInvalid, match="authorizes"):
        verify_capability(issued.token, operation=Tool.PYTHON_EXECUTE)


def test_a_capability_for_one_task_does_not_authorize_another(db, task, agent):
    issued = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    with pytest.raises(CapabilityInvalid, match="bound to task"):
        verify_capability(issued.token, operation=Tool.RAG_SEARCH, task_id="T-OTHER")
    with pytest.raises(CapabilityInvalid, match="bound to agent"):
        verify_capability(issued.token, operation=Tool.RAG_SEARCH, agent_id="A-OTHER")


def test_a_tampered_capability_is_refused(db, task, agent):
    issued = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    header, payload, signature = issued.token.split(".")
    with pytest.raises(CapabilityInvalid):
        verify_capability(f"{header}.{payload}.{signature[:-2]}xx", operation=Tool.RAG_SEARCH)
    with pytest.raises(CapabilityInvalid):
        verify_capability("not-a-token", operation=Tool.RAG_SEARCH)
    with pytest.raises(CapabilityInvalid):
        verify_capability("", operation=Tool.RAG_SEARCH)


def test_an_expired_capability_is_still_attributable(db, task, agent):
    """Only the clock failed -- the signature did not -- so the denial can be
    recorded against the task the token was genuinely issued for."""
    issued = issue_for_step(
        task.task_id, agent.agent_id, Tool.RAG_SEARCH, ttl_seconds=-1
    )
    with pytest.raises(CapabilityExpired) as caught:
        verify_capability(issued.token, operation=Tool.RAG_SEARCH)
    assert caught.value.capability is not None
    assert caught.value.capability.task_id == task.task_id


def test_a_session_token_is_not_a_capability_and_vice_versa(db, task, agent, admin):
    session_token, _ = issue_session_token(admin.user_id, admin.roles)
    with pytest.raises(CapabilityInvalid):
        verify_capability(session_token, operation=Tool.RAG_SEARCH)

    capability = issue_for_step(task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    with pytest.raises(SessionInvalid):
        verify_session_token(capability.token)


def test_a_capability_cannot_pair_an_agent_with_someone_elses_task(
    db, task, agent, internal_task
):
    with pytest.raises(CapabilityIssueError, match="belongs to task"):
        issue_for_step(internal_task.task_id, agent.agent_id, Tool.RAG_SEARCH)
    with pytest.raises(CapabilityIssueError, match="unknown task"):
        issue_for_step("T-NOPE", agent.agent_id, Tool.RAG_SEARCH)


def test_there_is_no_capability_revocation_list(db, task, agent):
    """BB-020: "No revocation list exists for the MVP -- the 5-minute TTL is
    the only expiry mechanism." Revocation's job is done by §6.8 instead, at
    the policy layer, which is why nothing here can revoke a token."""
    import app.capability as capability_package

    surface = {name for name in dir(capability_package) if not name.startswith("_")}
    assert not any("revoke" in name or "revocation" in name for name in surface)

    issued = issue_capability(
        task_id=task.task_id,
        agent_id=agent.agent_id,
        operation=Tool.RAG_SEARCH,
        scope=CapabilityScope(Classification.CONFIDENTIAL, "maintenance"),
    )
    # Nothing was stored: the token is self-contained and stateless.
    assert verify_capability(issued.token, operation=Tool.RAG_SEARCH)


# ==========================================================================
# The step-4 "done when" invariant
# ==========================================================================

_TOOL_NAMES = ("rag.search", "python.execute", "generate_report")

#: Modules allowed to spell a tool name out. `tools.py` defines the constants,
#: `engine.py` is §6.7's rule chain, and the gateway is the router.
_TOOL_NAME_ALLOWLIST = frozenset({"app/policy/tools.py", "app/policy/engine.py"})
_TOOL_NAME_ALLOWED_PREFIX = "app/tool_gateway/"


def _tool_name_literals(source: str) -> list[tuple[int, str]]:
    """String literals (never comments, never docstrings) naming a tool."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for name in _TOOL_NAMES:
            if name in node.value:
                found.append((node.lineno, name))
    return found


def test_no_code_path_invokes_a_tool_outside_the_gateway():
    """step 4's "Done when": no code path exists anywhere in the app for a
    tool to be invoked without going through the Tool Gateway.

    Enforced structurally rather than by inspection: a tool is reachable only
    through `app.tool_gateway.registry`, and a module that never names a tool
    cannot call one behind the gateway's back. Steps 5-8 import a constant from
    `app.policy.tools` and call `register_backend`; they never write the string.
    """
    root = Path(__file__).resolve().parent.parent

    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in _TOOL_NAME_ALLOWLIST or rel.startswith(_TOOL_NAME_ALLOWED_PREFIX):
            continue
        for lineno, name in _tool_name_literals(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}:{lineno} names {name!r}")

    assert offenders == [], (
        "a tool name is written as a literal outside the Policy Engine and the "
        "Tool Gateway: " + "; ".join(offenders) + ". Import the constant from "
        "app.policy.tools and attach a backend with "
        "app.tool_gateway.register_backend() instead."
    )


def test_the_tool_name_detector_is_not_vacuous():
    """Negative control for the test above -- it must actually catch a literal,
    and must not be fooled into flagging a docstring or a comment."""
    caught = _tool_name_literals('x = "rag.search"\n')
    assert caught == [(1, "rag.search")]
    assert _tool_name_literals('"""mentions rag.search"""\n# and python.execute\n') == []


def test_the_five_error_codes_are_a_closed_set():
    """§6.6 lists five codes for this slice; a sixth is a contract change."""
    from app.tool_gateway import ALL_ERROR_CODES, UnknownErrorCode
    from app.tool_gateway import envelope as envelope_module

    assert ALL_ERROR_CODES == {
        "CAPABILITY_INVALID", "CAPABILITY_EXPIRED", "POLICY_DENIED",
        "TOOL_DISABLED", "EXECUTION_ERROR",
    }
    with pytest.raises(UnknownErrorCode):
        envelope_module.failure("x", "APPROVAL_REQUIRED", "no", execution_id="EXEC1")
