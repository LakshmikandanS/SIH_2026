"""Route handlers. Each is a plain `async def handler(request: Request) ->
Response` -- Starlette's own shape, nothing wraps it -- kept in this module
so `app.py` is only the route table and app assembly: what an endpoint does
belongs beside the endpoint, not inferred from a router file that only
lists paths.

Every handler that needs to know who is calling starts with
`require_user(request, state)` (`deps.py`). The one exception,
`create_session`, is the identity-*issuing* endpoint -- see its own
docstring for why reading a claimed identity out of ITS body is not the
invariant-3 violation it would be anywhere else in this file.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from citadel_contracts.domain import Resource
from citadel_contracts.identity import issue_session_token
from citadel_platform.audit.chain import verify
from citadel_platform.audit.psql_client import PsqlChainSource, append_via_psql
from citadel_platform.identity.store import get_user_by_external_identity, list_users
from citadel_tools.policy import (
    PolicyEvaluationError,
    ReceiptFacts,
    actor_facts_from_user,
    evaluate,
)

from citadel_api.deps import AppState, require_user


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.citadel
    return state


async def health(request: Request) -> Response:
    """Liveness, plus just enough to eyeball from a browser tab: which
    profile this process loaded, proving the registry actually parsed."""
    state = _state(request)
    return JSONResponse(
        {"status": "ok", "service": "citadel-api", "profile": state.registry.profile.name}
    )


async def list_registry_tools(request: Request) -> Response:
    """The tool manifest, shaped for the UI's tool picker. Public: this is
    registry metadata (root AGENTS.md invariant 1), not a decision about
    any one actor, so it needs no session."""
    state = _state(request)
    tools = [
        {
            "name": t.name,
            "side_effect": t.side_effect,
            "required_capabilities": list(t.required_capabilities),
            "classification_ceiling": t.classification_ceiling,
            "requires_receipt": t.requires_receipt,
        }
        for t in state.registry.tools
    ]
    return JSONResponse({"tools": tools})


async def list_demo_users(request: Request) -> Response:
    """The M0 demo cast (migration 0006 / ADR-0001 §Q7), for the login
    picker. Deliberately unauthenticated and deliberately under
    `/api/demo/` -- this answers "who can I view the system as," not "who
    are our users"; a real identity provider would never expose this."""
    state = _state(request)
    users = list_users(state.pg_env)
    return JSONResponse(
        {
            "users": [
                {
                    "user_id": u.user_id,
                    "username": u.username,
                    "roles": list(u.roles),
                    "clearance": u.clearance,
                    "department": u.department,
                }
                for u in users
            ]
        }
    )


async def create_session(request: Request) -> Response:
    """Issue a session token for one of the seeded demo identities.

    This is the one handler in this service that reads an identity claim
    (`user_id`) out of its own request body, and it is not the invariant-3
    violation it would be anywhere else in this file: there is no session
    yet for this request to be verified against -- issuing one is this
    endpoint's entire job, the same way a real `/login` reads a claimed
    username before there is anything to check a bearer token against.

    What keeps this from meaning "claim to be anyone": M0 has no password
    or credential check at all (`citadel_contracts.domain.User` carries no
    such field -- see that module's own docstring on why), so
    `get_user_by_external_identity` IS the entire gate, and it only ever
    resolves to one of the three identities migration 0006 actually seeded.
    There is no identity to mint a session for that operations did not
    already provision. Every OTHER handler below resolves identity
    exclusively through `require_user()` -- a verified bearer token, never
    a body field.
    """
    state = _state(request)
    login_request: Any = await request.json()
    user_id = login_request.get("user_id") if isinstance(login_request, dict) else None
    if not isinstance(user_id, str) or not user_id:
        return JSONResponse({"error": "user_id is required"}, status_code=400)

    user = get_user_by_external_identity(state.pg_env, user_id)
    if user is None:
        return JSONResponse({"error": f"no such demo identity: {user_id!r}"}, status_code=404)

    token = issue_session_token(user, private_key=state.private_key)
    append_via_psql(
        state.pg_env,
        event_name="identity.verified",
        actor_id=user.user_id,
        payload={"username": user.username, "roles": list(user.roles)},
    )
    return JSONResponse(
        {
            "token": token,
            "user": {
                "user_id": user.user_id,
                "username": user.username,
                "roles": list(user.roles),
                "clearance": user.clearance,
                "department": user.department,
            },
        }
    )


async def whoami(request: Request) -> Response:
    """Round-trips the bearer token through `verify_session_token` and
    returns exactly what it asserts -- visible proof, from the UI, that
    identity comes from the token and never from anything the client
    sends."""
    state = _state(request)
    user = require_user(request, state)
    return JSONResponse(
        {
            "user_id": user.user_id,
            "username": user.username,
            "roles": list(user.roles),
            "clearance": user.clearance,
            "department": user.department,
        }
    )


async def try_policy(request: Request) -> Response:
    """The ACL/policy demonstration (ADR-0001 §Q7; web/AGENTS.md's "ACL
    surface"). Evaluates the real `registry/policy.yaml` rules -- the same
    pure `citadel_tools.policy.evaluate()` the not-yet-built chokepoint will
    call -- against the caller's *actual* verified identity and a resource
    the caller describes; the caller only ever chooses which resource and
    tool to try, never their own role or clearance. Writes a real audit
    event for the decision either way (PLAN-M0 task 8's third Done clause),
    taking over that one responsibility from the chokepoint until it
    exists: a documented stand-in, not a permanent home for it (see
    `packages/tools/AGENTS.md`).

    `receipt.valid` is always `False` here: nothing in this checkpoint
    issues or verifies a receipt yet (M3-M4 work). A tool with
    `requires_receipt: true` (docs.search, docs.read, code.run,
    vision.extract, doc.generate) will therefore deny on
    `deny-missing-receipt` once it clears clearance/ACL/tool-ceiling --
    an accurate reflection of what is and is not built yet, not a bug here.
    Try `fs.read`, `sheet.read` or `calc.evaluate` (`requires_receipt:
    false`) to see a real allow.
    """
    state = _state(request)
    user = require_user(request, state)

    try_request: Any = await request.json()
    if not isinstance(try_request, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

    tool_name = try_request.get("tool")
    if not isinstance(tool_name, str) or not tool_name:
        return JSONResponse({"error": "tool is required"}, status_code=400)
    try:
        tool = state.registry.tool(tool_name)
    except KeyError:
        return JSONResponse({"error": f"no such tool: {tool_name!r}"}, status_code=404)

    resource_fields = try_request.get("resource")
    if not isinstance(resource_fields, dict):
        resource_fields = {}
    resource = Resource.build(
        resource_id=str(resource_fields.get("resource_id") or "demo-resource"),
        type=str(resource_fields.get("type") or "document"),
        # Uppercased here, at the boundary that turns a caller-supplied value
        # into a policy fact -- the same normalisation registry/schema.py's
        # _known_classification applies to registry YAML and
        # actor_facts_from_user applies to User.clearance, applied a third
        # time to the one remaining source of a classification string this
        # service accepts. citadel_contracts.classification.Classification's
        # _ORDER keys are uppercase-only with no case folding of its own, so
        # skipping this would make "confidential" (as a demo caller would
        # naturally type it) fail deny-unknown-classification instead of
        # being compared for real -- silently misleading, not stricter. A
        # value that still isn't a real level after uppercasing (try
        # "restricted") correctly reaches that rule anyway, which is exactly
        # the fail-closed-on-unknown-marking behaviour it exists to show.
        classification=str(resource_fields.get("classification") or "internal").upper(),
        acl=[str(entry) for entry in (resource_fields.get("acl") or [])],
    )

    try:
        actor = actor_facts_from_user(user, state.registry)
    except PolicyEvaluationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    receipt = ReceiptFacts(valid=False)
    decision = evaluate(
        state.registry.policy, actor=actor, resource=resource, tool=tool, receipt=receipt
    )

    seq = append_via_psql(
        state.pg_env,
        event_name="policy.decision",
        actor_id=user.user_id,
        payload={
            "tool": tool.name,
            "resource": {
                "resource_id": resource.resource_id,
                "type": resource.type,
                "classification": resource.classification,
                "acl": list(resource.acl),
            },
            "effect": decision.effect,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
        },
    )

    return JSONResponse(
        {
            "decision": {
                "effect": decision.effect,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
            },
            "audit_seq": seq,
            "actor": {
                "role": actor.role,
                "department": actor.department,
                "classification_max": actor.classification_max,
                "capabilities": list(actor.capabilities),
            },
        }
    )


async def recent_audit(request: Request) -> Response:
    """The tail of the real audit chain, oldest first, newest last -- any
    signed-in demo identity may view it. (Gating this by role/capability is
    a real next step; `registry/roles.yaml` defines no audit-access
    capability yet, and adding one is out of scope for this checkpoint.)"""
    state = _state(request)
    require_user(request, state)

    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 200))

    rows = PsqlChainSource(env=state.pg_env).rows()
    tail = rows[-limit:]
    return JSONResponse(
        {
            "total": len(rows),
            "events": [
                {
                    "seq": row.seq,
                    "event_name": row.event_name,
                    "occurred_at": row.occurred_at_text,
                    "actor_id": row.actor_id,
                    "payload": json.loads(row.payload_text),
                    "row_hash": row.row_hash.hex(),
                }
                for row in tail
            ],
        }
    )


async def verify_audit(request: Request) -> Response:
    """Recomputes the whole chain right now and reports whether it is
    intact -- `citadel_platform.audit.chain.verify`, the same function
    `test_audit_chain_schema.py` proves catches a row edited in place after
    the anti-tamper trigger is disabled."""
    state = _state(request)
    require_user(request, state)

    rows = PsqlChainSource(env=state.pg_env).rows()
    result = verify(rows)
    return JSONResponse(
        {
            "ok": result.ok,
            "checked": len(rows),
            "first_break_seq": result.first_break_seq,
            "reason": result.reason,
        }
    )


__all__ = [
    "health",
    "list_registry_tools",
    "list_demo_users",
    "create_session",
    "whoami",
    "try_policy",
    "recent_audit",
    "verify_audit",
]
