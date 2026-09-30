"""The Tool Gateway -- the authorization spine (design doc §6.6).

    Agent -> Tool Gateway
               |
               +- Step A: verify capability (signature, expiry, operation
               |          match) -- LOCAL, no network call
               |
               +- Step B: policy decision -- Tool Gateway calls Control Plane's
               |          Policy Engine ONCE per call, with the concrete
               |          resource now known (capability only proves "this
               |          agent may attempt rag.search in general"; policy
               |          decides "is this specific document allowed right now")
               |
               +- Step C: route to the right backend by `tool` name (Data
                          Plane, or Execution Service), and return one uniform
                          envelope regardless of which backend answered

A and B are two checks and stay two checks (C-002). Collapsing them would
recreate exactly the contradiction §6.6 was written to resolve: a capability is
minted before the step runs, when the concrete resource is not yet known, so it
can only ever authorize a *class* of action. The resource-dependent question is
answered here, at call time, on every single call -- which is also why
`DISABLE TOOL` (§6.8) works against a still-valid token.

Identity, throughout, is derived and never accepted:

  * the acting `task_id`/`agent_id` come from the *verified capability*, not
    from an argument, so a caller cannot act on another task with a good token;
  * the acting user is then loaded from that task's `user_id`;
  * the resource descriptor is supplied by the trusted caller (the Orchestrator
    in step 7) from the concrete target -- never from the agent's own
    `arguments`, which would let an agent describe its target as harmless.

Events. This module is the one chokepoint, so it is the one emitter:
`CAPABILITY_CHECKED` after Step A, `POLICY_DECISION` after Step B,
`TOOL_DENIED` on any non-ALLOW outcome, `TOOL_EXECUTED` once Step C has
dispatched. Backends emit none of them. Events are written on the writer's own
transaction, never enlisted in a caller's: a denial must survive the caller
rolling its work back.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app import ids
from app.capability.tokens import (
    Capability,
    CapabilityExpired,
    CapabilityInvalid,
    verify_capability,
)
from app.db.engine import SessionLocal
from app.db.models import Agent, Task, User
from app.observability import EventType, append_event
from app.policy.context import (
    PolicyAction,
    PolicyAgent,
    PolicyResource,
    PolicyTask,
    PolicyUser,
)
from app.policy.engine import Decision, PolicyOutcome, Rule, evaluate
from app.tool_gateway import envelope
from app.tool_gateway.envelope import ErrorCode
from app.tool_gateway.registry import ToolNotRegistered, ToolRequest, get_backend


class _CallerContext:
    """Resolved §6.7 inputs for one call, all derived from the capability."""

    __slots__ = ("user", "agent", "task")

    def __init__(self, user: PolicyUser, agent: PolicyAgent, task: PolicyTask) -> None:
        self.user = user
        self.agent = agent
        self.task = task


def invoke(
    *,
    capability_token: str,
    tool: str,
    resource: PolicyResource,
    arguments: Optional[Mapping[str, Any]] = None,
    session: Optional[Session] = None,
) -> dict[str, Any]:
    """Run one tool call through Step A, Step B and Step C.

    THE contract steps 5-8 wire through. Always returns a §6.6 envelope; it
    does not raise for an authorization or execution failure, because "denied"
    is a first-class outcome (§1.2), not an exception.

    Args:
        capability_token: the signed capability for THIS step (§6.5), issued
            immediately before the call by `app.capability.issue_for_step`.
            Its `operation` must equal `tool`.
        tool: the canonical tool name -- use a constant from
            `app.policy.tools.Tool`, never a literal.
        resource: the concrete thing being acted on, with its classification
            and ACL. Required, deliberately: §6.7's two data rules are
            evaluated against it, and a default would silently pass them. For
            a tool that acts only within the task's own envelope, build one
            with `task_resource(task_id)`.
        arguments: the action's arguments, passed through to the backend.
        session: an optional read session, so a caller inside a transaction
            can see its own uncommitted Task/Agent rows. Events are never
            written on it.

    Returns:
        The §6.6 envelope: five keys, success or failure.
    """
    execution_id = ids.new_id(ids.EXECUTION)
    args: dict[str, Any] = dict(arguments or {})

    # ---- Step A: verify the capability. Local, no network call. ------------
    try:
        capability = verify_capability(capability_token, operation=tool)
    except CapabilityExpired as exc:
        return _capability_rejected(
            tool, ErrorCode.CAPABILITY_EXPIRED, str(exc), exc.capability, execution_id
        )
    except CapabilityInvalid as exc:
        # Nothing about an unverifiable token is trustworthy, including the
        # task it claims to belong to, so the event is system-scoped.
        return _capability_rejected(
            tool, ErrorCode.CAPABILITY_INVALID, str(exc), None, execution_id
        )

    append_event(
        capability.task_id,
        capability.agent_id,
        EventType.CAPABILITY_CHECKED,
        {
            "valid": True,
            "capability_id": capability.capability_id,
            "operation": capability.operation,
            "tool": tool,
            "scope": capability.scope.to_claim(),
            "expires_at": capability.expires_at.isoformat(),
            "execution_id": execution_id,
        },
    )

    # The capability binds a task/agent pair; everything else is looked up
    # from it. No caller-supplied identity enters here (§6.4).
    try:
        context = _resolve_context(capability, session)
    except _ContextUnresolvable as exc:
        return _denied(
            tool=tool,
            capability=capability,
            code=ErrorCode.CAPABILITY_INVALID,
            rule="capability_binding_unresolvable",
            reason=str(exc),
            resource=resource,
            arguments=args,
            execution_id=execution_id,
        )

    # ---- Step B: ONE policy decision, with the concrete resource known. ----
    action = PolicyAction(tool=tool, arguments=args)
    outcome: PolicyOutcome = evaluate(
        context.user, context.agent, context.task, action, resource
    )

    append_event(
        capability.task_id,
        capability.agent_id,
        EventType.POLICY_DECISION,
        {
            "decision": outcome.decision,
            "rule": outcome.rule,
            "reason": outcome.reason,
            "tool": tool,
            "capability_id": capability.capability_id,
            "resource": _resource_payload(resource),
            "task_classification": context.task.classification,
            "task_department": context.task.department,
            "execution_id": execution_id,
        },
    )

    if outcome.decision != Decision.ALLOW:
        # §6.8's emergency control gets its own error code; every other
        # non-ALLOW outcome is POLICY_DENIED. REQUIRE_APPROVAL lands here too:
        # the tool is not executed, and §6.6 has no approval error code, so it
        # is reported as a denial that names the route a human must take
        # (§6.10's approval endpoint). Fail closed.
        code = (
            ErrorCode.TOOL_DISABLED
            if outcome.rule == Rule.TOOL_DISABLED
            else ErrorCode.POLICY_DENIED
        )
        message = outcome.reason
        if outcome.decision == Decision.REQUIRE_APPROVAL:
            message = (
                f"{outcome.reason}; it is not executable as a tool call -- use "
                f"the approval endpoint (§6.10)"
            )
        return _denied(
            tool=tool,
            capability=capability,
            code=code,
            rule=outcome.rule,
            reason=message,
            resource=resource,
            arguments=args,
            execution_id=execution_id,
            decision=outcome.decision,
        )

    # ---- Step C: route to the backend, return the uniform envelope. --------
    request = ToolRequest(
        tool=tool,
        arguments=args,
        capability=capability,
        user=context.user,
        agent=context.agent,
        task=context.task,
        resource=resource,
        execution_id=execution_id,
    )

    try:
        backend = get_backend(tool)
    except ToolNotRegistered as exc:
        return _execution_failed(tool, capability, str(exc), resource, execution_id)

    try:
        result = backend(request)
    except Exception as exc:  # noqa: BLE001 -- any backend failure, uniformly
        return _execution_failed(
            tool,
            capability,
            f"{type(exc).__name__}: {exc}",
            resource,
            execution_id,
        )

    append_event(
        capability.task_id,
        capability.agent_id,
        EventType.TOOL_EXECUTED,
        {
            "success": True,
            "tool": tool,
            "capability_id": capability.capability_id,
            "resource": _resource_payload(resource),
            "execution_id": execution_id,
        },
    )
    return envelope.success(tool, result, execution_id=execution_id)


def task_resource(
    task_id: str,
    *,
    type: str = "task_scope",
    session: Optional[Session] = None,
) -> PolicyResource:
    """Build the resource descriptor for a tool acting only within its task.

    `python.execute` computing over evidence already retrieved, or
    `generate_report` rendering it, have no external target -- but §6.7's two
    data rules must still run, so they are evaluated against the task's own
    classification and department. Use this rather than inventing a
    permissive descriptor; a tool that *does* touch an external resource must
    describe that resource instead.
    """
    if session is not None:
        return PolicyResource.for_task(_policy_task(session, task_id), type=type)
    with SessionLocal() as own:
        return PolicyResource.for_task(_policy_task(own, task_id), type=type)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


class _ContextUnresolvable(Exception):
    """A verified capability points at a task/agent/user that is not there."""


def _resolve_context(
    capability: Capability, session: Optional[Session]
) -> _CallerContext:
    if session is not None:
        return _load_context(session, capability)
    with SessionLocal() as own:
        return _load_context(own, capability)


def _load_context(session: Session, capability: Capability) -> _CallerContext:
    task = session.get(Task, capability.task_id)
    if task is None:
        raise _ContextUnresolvable(f"capability names unknown task {capability.task_id!r}")

    agent = session.get(Agent, capability.agent_id)
    if agent is None:
        raise _ContextUnresolvable(
            f"capability names unknown agent {capability.agent_id!r}"
        )
    if agent.task_id != task.task_id:
        raise _ContextUnresolvable(
            f"agent {agent.agent_id!r} is not the agent of task {task.task_id!r}"
        )

    owner = session.get(User, task.user_id)
    if owner is None:
        raise _ContextUnresolvable(f"task {task.task_id!r} has no owning user")

    return _CallerContext(
        user=PolicyUser.from_row(owner),
        agent=PolicyAgent.from_row(agent),
        task=PolicyTask.from_rows(task, owner),
    )


def _policy_task(session: Session, task_id: str) -> PolicyTask:
    task = session.get(Task, task_id)
    if task is None:
        raise _ContextUnresolvable(f"unknown task {task_id!r}")
    owner = session.get(User, task.user_id)
    if owner is None:
        raise _ContextUnresolvable(f"task {task_id!r} has no owning user")
    return PolicyTask.from_rows(task, owner)


def _resource_payload(resource: PolicyResource) -> dict[str, Any]:
    return {
        "resource_id": resource.resource_id,
        "type": resource.type,
        "classification": resource.classification,
        "acl": list(resource.acl),
    }


def _capability_rejected(
    tool: str,
    code: str,
    message: str,
    capability: Optional[Capability],
    execution_id: str,
) -> dict[str, Any]:
    """Step A failed. Record the check, record the denial, execute nothing.

    Step B is not consulted: the checks are ordered and a call that cannot
    prove it was authorized to attempt the operation never reaches the
    question of whether this particular resource is allowed.
    """
    task_id = capability.task_id if capability else None
    actor_id = capability.agent_id if capability else None
    detail: dict[str, Any] = {
        "valid": False,
        "code": code,
        "reason": message,
        "tool": tool,
        "execution_id": execution_id,
    }
    if capability is not None:
        detail["capability_id"] = capability.capability_id
        detail["operation"] = capability.operation
        detail["expires_at"] = capability.expires_at.isoformat()

    append_event(task_id, actor_id, EventType.CAPABILITY_CHECKED, detail)
    append_event(
        task_id,
        actor_id,
        EventType.TOOL_DENIED,
        {
            "tool": tool,
            "code": code,
            "reason": message,
            "stage": "capability",
            "execution_id": execution_id,
        },
    )
    return envelope.failure(tool, code, message, execution_id=execution_id)


def _denied(
    *,
    tool: str,
    capability: Capability,
    code: str,
    rule: str,
    reason: str,
    resource: PolicyResource,
    arguments: Mapping[str, Any],
    execution_id: str,
    decision: str = Decision.DENY,
) -> dict[str, Any]:
    """Step B (or the binding check) refused. The backend is never reached."""
    append_event(
        capability.task_id,
        capability.agent_id,
        EventType.TOOL_DENIED,
        {
            "tool": tool,
            "code": code,
            "decision": decision,
            "rule": rule,
            "reason": reason,
            "capability_id": capability.capability_id,
            "resource": _resource_payload(resource),
            "arguments": dict(arguments),
            "stage": "policy",
            "execution_id": execution_id,
        },
    )
    return envelope.failure(tool, code, reason, execution_id=execution_id)


def _execution_failed(
    tool: str,
    capability: Capability,
    message: str,
    resource: PolicyResource,
    execution_id: str,
) -> dict[str, Any]:
    """Step C was reached and failed.

    Recorded as `TOOL_EXECUTED` with `success: false`, not as `TOOL_DENIED`:
    authorization *succeeded* here, and labelling an execution fault a denial
    would corrupt the one signal §1.2 and §1.3 rest on. §6.12's vocabulary is
    closed and has no third tool-outcome type, and leaving an authorized call
    unrecorded would be worse than either.
    """
    append_event(
        capability.task_id,
        capability.agent_id,
        EventType.TOOL_EXECUTED,
        {
            "success": False,
            "tool": tool,
            "capability_id": capability.capability_id,
            "resource": _resource_payload(resource),
            "error": {"code": ErrorCode.EXECUTION_ERROR, "message": message},
            "execution_id": execution_id,
        },
    )
    return envelope.failure(
        tool, ErrorCode.EXECUTION_ERROR, message, execution_id=execution_id
    )
