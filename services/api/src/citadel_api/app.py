"""Starlette application assembly: the route table, exception handling, and serving the
web UI from the same origin as the API.

**Starlette, not FastAPI.** FastAPI could not be installed where this was first built
(PyPI network-blocked); Starlette is FastAPI's own foundation, so this is a
substitution of implementation, not of architecture.

**Same origin, on purpose.** The web UI (`web/src/`) is mounted at `/` on this same app,
after every `/api/...` route -- no CORS surface, no second process.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from citadel_platform._psql import PsqlError

from citadel_api import (
    handlers,
    routes_artifacts,
    routes_documents,
    routes_memory,
    routes_observe,
    routes_system,
    routes_tasks,
    routes_workbench,
)
from citadel_api.deps import AppState, AuthError, Forbidden, load_app_state


async def _handle_auth_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AuthError)
    return JSONResponse({"error": exc.message}, status_code=401)


async def _handle_forbidden(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, Forbidden)
    return JSONResponse({"error": exc.message}, status_code=403)


async def _handle_psql_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, PsqlError)
    # Never the raw psql stderr to a client -- it can echo SQL text back.
    print(f"[citadel-api] database error: {exc}", flush=True)
    return JSONResponse({"error": "database unavailable"}, status_code=503)


def routes() -> list[Any]:
    return [
        Route("/api/health", handlers.health, methods=["GET"]),
        Route("/api/registry/tools", handlers.list_registry_tools, methods=["GET"]),
        Route("/api/registry/templates", routes_system.templates, methods=["GET"]),
        Route("/api/demo/users", handlers.list_demo_users, methods=["GET"]),
        Route("/api/auth/session", handlers.create_session, methods=["POST"]),
        Route("/api/me", handlers.whoami, methods=["GET"]),
        Route("/api/policy/try", handlers.try_policy, methods=["POST"]),
        Route("/api/audit/recent", handlers.recent_audit, methods=["GET"]),
        Route("/api/audit/verify", handlers.verify_audit, methods=["GET"]),
        # tasks
        Route("/api/tasks", routes_tasks.tasks_index, methods=["GET"]),
        Route("/api/tasks", routes_tasks.create_task, methods=["POST"]),
        Route("/api/tasks/{task_id}", routes_tasks.task_detail, methods=["GET"]),
        Route("/api/tasks/{task_id}/events", routes_tasks.task_events, methods=["GET"]),
        Route("/api/tasks/{task_id}/cancel", routes_tasks.cancel_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/probe", routes_tasks.probe_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/sovereignty", routes_tasks.task_sovereignty, methods=["GET"]),
        Route("/api/tasks/{task_id}/workspace", routes_tasks.task_workspace, methods=["GET"]),
        Route("/api/tasks/{task_id}/workspace/{path:path}", routes_tasks.task_workspace, methods=["GET"]),
        # people working on a task alongside its agents
        Route("/api/tasks/{task_id}/revise", routes_workbench.revise_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/pause", routes_workbench.pause_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/resume", routes_workbench.resume_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/notes", routes_workbench.add_note, methods=["POST"]),
        Route("/api/tasks/{task_id}/tools/{tool}", routes_workbench.run_tool, methods=["POST"]),
        Route("/api/tasks/{task_id}/state", routes_workbench.task_state, methods=["GET"]),
        # the workbench: drafts, activity, tools, environment
        Route("/api/drafts", routes_workbench.drafts_index, methods=["GET"]),
        Route("/api/drafts", routes_workbench.create_draft, methods=["POST"]),
        Route("/api/drafts/{draft_id}", routes_workbench.update_draft, methods=["PUT"]),
        Route("/api/drafts/{draft_id}", routes_workbench.delete_draft, methods=["DELETE"]),
        Route("/api/drafts/{draft_id}/commit", routes_workbench.commit_draft, methods=["POST"]),
        Route("/api/activity", routes_workbench.activity, methods=["GET"]),
        Route("/api/workbench/tools", routes_workbench.tools_status, methods=["GET"]),
        Route("/api/workbench/environment", routes_workbench.environment, methods=["GET"]),
        # what the workbench remembers
        Route("/api/memory", routes_memory.memory_index, methods=["GET"]),
        Route("/api/memory", routes_memory.remember, methods=["POST"]),
        Route("/api/memory/events", routes_memory.memory_events, methods=["GET"]),
        Route("/api/memory/{memory_id}", routes_memory.edit_memory, methods=["PUT"]),
        Route("/api/memory/{memory_id}/status", routes_memory.set_memory_status, methods=["POST"]),
        # documents and search
        Route("/api/documents", routes_documents.documents_index, methods=["GET"]),
        Route("/api/documents", routes_documents.upload_document, methods=["POST"]),
        Route("/api/documents/{document_id}/pages/{page:int}", routes_documents.document_page, methods=["GET"]),
        Route("/api/documents/{document_id}/pages/{page:int}/image", routes_documents.document_page_image, methods=["GET"]),
        Route("/api/documents/{document_id}/versions", routes_documents.document_history, methods=["GET"]),
        Route("/api/documents/{document_id}/versions", routes_documents.reissue_document, methods=["POST"]),
        Route("/api/documents/{document_id}/diff", routes_documents.document_diff, methods=["GET"]),
        Route("/api/search", routes_documents.search_documents, methods=["POST"]),
        # artifacts and approvals
        Route("/api/artifacts", routes_artifacts.artifacts_index, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}", routes_artifacts.artifact_detail, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}/download", routes_artifacts.artifact_download, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}/preview", routes_artifacts.artifact_preview, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}/provenance", routes_artifacts.artifact_provenance, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}/decision", routes_artifacts.decide_artifact, methods=["POST"]),
        Route("/api/artifacts/{artifact_id}/content", routes_artifacts.artifact_content, methods=["GET"]),
        Route("/api/artifacts/{artifact_id}/edit", routes_artifacts.edit_artifact, methods=["POST"]),
        # models, routing, sovereignty, observability
        Route("/api/models", routes_system.models_status, methods=["GET"]),
        Route("/api/models/pull", routes_system.pull_models, methods=["POST"]),
        Route("/api/routing/preview", routes_system.routing_preview, methods=["POST"]),
        Route("/api/sovereignty", routes_system.sovereignty_panel, methods=["GET"]),
        Route("/api/sovereignty/probe", routes_system.sovereignty_probe, methods=["POST"]),
        Route("/api/metrics", routes_system.metrics, methods=["GET"]),
        Route("/api/traces", routes_system.traces, methods=["GET"]),
        Route("/api/observability", routes_observe.observability, methods=["GET"]),
    ]


def create_app(state: Optional[AppState] = None) -> Starlette:
    """Build one Starlette app: load `AppState` once, register every route, and mount
    the web UI's static files at `/` last, so the UI can never shadow an API route."""
    app_state = state or load_app_state()
    table = routes()
    if app_state.web_dir.is_dir():
        table.append(Mount("/", app=StaticFiles(directory=str(app_state.web_dir), html=True), name="web"))
    app = Starlette(
        routes=table,
        exception_handlers={
            AuthError: _handle_auth_error,
            Forbidden: _handle_forbidden,
            PsqlError: _handle_psql_error,
        },
    )
    app.state.citadel = app_state
    return app


__all__ = ["create_app", "routes"]
