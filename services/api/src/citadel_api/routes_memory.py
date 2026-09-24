"""What the workbench remembers, for the people who work in it.

The memory manager (packages/memory, docs/adr/0009) grounds every plan in what earlier
work established; this is where a person sees that memory, searches it, states
something it should know, corrects it, archives what is no longer true -- and reads the
manager's own decisions (created, merged, updated, contradicted, ignored...). Every read
is filtered by the person's department and clearance in the same SQL statement as the
search, exactly as the agents' recalls are.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from citadel_contracts.domain import User
from citadel_memory import SUGGESTED_TYPES, TIERS, Compartment, MemoryManager, MemoryScope

from citadel_api.common import blocking, effective_level, error, json_body, state
from citadel_api.deps import require_user


def _scope(user: User) -> MemoryScope:
    return MemoryScope(department=user.department, classification_max=user.clearance.upper(), actor_id=user.user_id)


async def memory_index(request: Request) -> Response:
    """Browse (newest first) or, with `q`, recall by meaning and recency -- without
    counting the look as a use, so browsing does not reorder what agents recall."""
    app = state(request)
    user = require_user(request, app)
    query = (request.query_params.get("q") or "").strip()
    status = request.query_params.get("status") or "active"
    if status not in ("active", "superseded", "archived"):
        return error("status is active, superseded or archived")
    manager = MemoryManager(app.db, app.gateway, app.audit)

    def gather() -> dict[str, Any]:
        if query:
            found = manager.recall(query, _scope(user), top_k=20, touch=False)
        else:
            found = manager.browse(_scope(user), status=status, limit=200)
        return {
            "memories": [m.to_dict() for m in found],
            "stats": manager.stats(_scope(user)),
            "scope": {"department": user.department, "clearance": user.clearance.upper()},
            "types": list(SUGGESTED_TYPES), "tiers": list(TIERS),
        }

    return JSONResponse(await blocking(gather))


async def memory_events(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    rows = await blocking(MemoryManager(app.db).events, _scope(user), limit=80,
                          task_id=request.query_params.get("task_id") or None)
    return JSONResponse({"events": rows})


async def remember(request: Request) -> Response:
    """A person states something the workbench should know. It goes through the same
    mutation flow as an agent's memory, so saying a known thing again merges, not repeats."""
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    content = " ".join(str(body.get("content") or "").split())
    if not 8 <= len(content) <= 600:
        return error("a memory is one statement of 8 to 600 characters")
    level = effective_level(user, body.get("classification"))
    compartment = Compartment(level, (user.department,))
    manager = MemoryManager(app.db, app.gateway, app.audit)
    outcome = await blocking(manager.remember, content, compartment=compartment, actor_id=user.user_id,
                             memory_type=str(body.get("memory_type") or "lesson"),
                             tier=str(body.get("tier") or "semantic"),
                             subject=str(body.get("subject") or "").strip() or None)
    return JSONResponse(outcome.to_dict(), status_code=201)


async def edit_memory(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    content = " ".join(str(body.get("content") or "").split())
    if not 8 <= len(content) <= 600:
        return error("a memory is one statement of 8 to 600 characters")
    changed = await blocking(MemoryManager(app.db, app.gateway, app.audit).edit, request.path_params["memory_id"],
                             content, scope=_scope(user), actor_id=user.user_id)
    return JSONResponse({"edited": True}) if changed else error("no such memory, or not one you may see", 404)


async def set_memory_status(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    status = str(body.get("status") or "")
    if status not in ("active", "archived"):
        return error("a person may archive a memory ('archived') or restore it ('active')")
    changed = await blocking(MemoryManager(app.db, app.gateway, app.audit).set_status, request.path_params["memory_id"],
                             status, scope=_scope(user), actor_id=user.user_id)
    return JSONResponse({"status": status}) if changed else error("no such memory, or not one you may see", 404)


__all__ = ["memory_index", "memory_events", "remember", "edit_memory", "set_memory_status"]
