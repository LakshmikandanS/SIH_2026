"""The system surfaces: models and routing (target A), sovereignty (target E), metrics,
traces and the template registry."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from citadel_gateway import ProviderError, RoutingRequest
from citadel_platform.tracing import metrics_summary
from citadel_sovereignty import enforcement_status, status as sovereignty_status

from citadel_api.common import blocking, effective_level, error, json_body, state
from citadel_api.deps import require_role, require_user
from citadel_api.routes_tasks import run_deliberate_probe

#: The requests the routing panel previews side by side: what the runtime actually asks
#: the gateway for when it plans, acts on an ordinary task, acts on a code task, and
#: re-reads a scanned region. Phrased as needs, never as model names.
ROUTING_SCENARIOS: list[dict[str, Any]] = [
    {"label": "Planning a task", "purpose": "plan", "required_capabilities": ["planning", "structured_output"],
     "preferred_capability": "planning"},
    {"label": "Acting on a reasoning task", "purpose": "act", "required_capabilities": ["tool_calling", "structured_output"],
     "preferred_capability": "reasoning"},
    {"label": "Acting on a code task", "purpose": "act", "required_capabilities": ["tool_calling", "structured_output"],
     "preferred_capability": "code_generation"},
    {"label": "Re-reading a scanned region", "purpose": "vision.extract", "required_capabilities": ["vision"],
     "modalities": ["text", "image"]},
    {"label": "Embedding a query", "purpose": "embed", "required_capabilities": ["embedding"]},
]


async def models_status(request: Request) -> Response:
    app = state(request)
    require_user(request, app)
    status = await blocking(app.gateway.status)
    status["missing"] = [m["id"] for m in status["models"] if m["enabled"] and not m["installed"]]
    return JSONResponse(status)


async def pull_models(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    require_role(user, "engineer", "admin")
    body = await json_body(request)
    ids = body.get("model_ids")
    try:
        started = await blocking(app.gateway.pull, list(ids) if isinstance(ids, list) else None)
    except ProviderError as exc:
        return error(str(exc), 503)
    await blocking(app.audit.record, "model.pull_requested", actor_id=user.user_id, payload={"models": started})
    return JSONResponse({"started": started})


async def routing_preview(request: Request) -> Response:
    """Every scenario, routed right now against the live runtime: the selection, the
    fallback chain, and every candidate's score breakdown or reason for ineligibility."""
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    level = effective_level(user, body.get("classification"))
    requested = body.get("scenarios")
    scenarios: list[Any] = requested if isinstance(requested, list) else ROUTING_SCENARIOS

    def route_all() -> list[dict[str, Any]]:
        results = []
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            request_ = RoutingRequest(
                purpose=str(scenario.get("purpose") or "act"),
                required_capabilities=tuple(str(c) for c in scenario.get("required_capabilities") or ()),
                modalities=tuple(str(m) for m in scenario.get("modalities") or ("text",)),
                classification=level,
                preferred_capability=scenario.get("preferred_capability"),
                context_estimate=int(scenario.get("context_estimate") or 4096),
            )
            decision = app.gateway.route(request_)
            results.append({"label": scenario.get("label") or request_.purpose, "decision": decision.to_dict(),
                            "candidates": [{**c.to_dict(), "summary": c.summary()} for c in decision.candidates]})
        return results

    return JSONResponse({"classification": level, "scenarios": await blocking(route_all)})


async def sovereignty_panel(request: Request) -> Response:
    app = state(request)
    require_user(request, app)

    def gather() -> dict[str, Any]:
        sandbox = app.sandbox.health() if app.sandbox is not None else {
            "ok": True, "kind": "process", "network": "in-process guard only (no sandbox container on this deployment)"}
        return {
            "status": sovereignty_status(app.db),
            "api_process": app.sovereignty.describe() if app.sovereignty else None,
            "sandbox": sandbox,
            "enforcement": enforcement_status(),
            "profile": {"name": app.registry.profile.name, "sovereign": app.registry.profile.sovereign,
                        "inference_endpoint": getattr(app.gateway.provider, "endpoint", None)},
        }

    return JSONResponse(await blocking(gather))


async def sovereignty_probe(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    report = await run_deliberate_probe(request, task_id=None, actor_id=user.user_id)
    return JSONResponse(report)


async def metrics(request: Request) -> Response:
    app = state(request)
    require_user(request, app)

    def gather() -> dict[str, Any]:
        return {
            "spans": metrics_summary(app.db, window_minutes=int(request.query_params.get("minutes", "60"))),
            "gpu_admission": app.gateway.gpu.snapshot(),
            "tasks": app.db.query("SELECT status, count(*) AS n FROM tasks GROUP BY status ORDER BY status"),
            "documents": app.db.query("SELECT status, count(*) AS n FROM documents GROUP BY status ORDER BY status"),
            "artifacts": app.db.query("SELECT status, count(*) AS n FROM artifacts GROUP BY status ORDER BY status"),
            "models": app.db.query(
                "SELECT attributes ->> 'model_id' AS model_id, count(*) AS calls, round(avg(duration_ms)) AS avg_ms, "
                "sum(coalesce((attributes -> 'usage' ->> 'prompt')::int, 0)) AS prompt_tokens, "
                "sum(coalesce((attributes -> 'usage' ->> 'completion')::int, 0)) AS completion_tokens "
                "FROM trace_spans WHERE kind = 'model' AND attributes ? 'model_id' GROUP BY 1 ORDER BY 2 DESC"
            ),
        }

    return JSONResponse(await blocking(gather))


async def traces(request: Request) -> Response:
    app = state(request)
    require_user(request, app)
    task_id = request.query_params.get("task_id")
    if task_id:
        rows = await blocking(
            app.db.query,
            "SELECT span_id::text AS span_id, parent_id::text AS parent_id, name, kind, started_at, duration_ms, status, "
            "attributes - 'routing' AS attributes FROM trace_spans WHERE task_id = %(t)s::uuid ORDER BY started_at",
            {"t": task_id},
        )
    else:
        rows = await blocking(
            app.db.query,
            "SELECT span_id::text AS span_id, task_id::text AS task_id, name, kind, started_at, duration_ms, status "
            "FROM trace_spans ORDER BY started_at DESC LIMIT 100",
        )
    return JSONResponse({"spans": rows})


async def templates(request: Request) -> Response:
    app = state(request)
    require_user(request, app)
    return JSONResponse({"templates": [
        {"id": t.id, "format": t.format, "approval_block": t.approval_block, "revision_history": t.revision_history,
         "sections": [{"key": s.key, "type": s.type, "required": s.required, "cited": s.cited} for s in t.sections]}
        for t in app.registry.templates
    ]})


__all__ = [
    "models_status",
    "pull_models",
    "routing_preview",
    "sovereignty_panel",
    "sovereignty_probe",
    "metrics",
    "traces",
    "templates",
    "ROUTING_SCENARIOS",
]
