"""Starlette application assembly: the route table, exception handling, and
serving the web UI from the same origin as the API.

**Starlette, not FastAPI.** FastAPI cannot be installed in the sandbox this
was first built in -- PyPI (`pypi.org`, `files.pythonhosted.org`) is
network-blocked there, confirmed empirically (`pip install fastapi uvicorn`
fails with no matching distribution), and only `starlette`/`uvicorn`/
`flask` are pre-installed. Starlette is FastAPI's own foundation and keeps
the ASGI/SSE door PLAN-M0 task 10 and `services/AGENTS.md` already point at
open for whenever a real dependency install is available -- this is a
substitution of implementation, not of architecture. See `citadel_api`'s
package docstring for the other sandbox-driven substitution (`psql`, not
`psycopg`) and why neither is a permanent design decision.

**Same origin, on purpose.** The web UI (`web/src/`) is mounted at `/` on
this same app, after every `/api/...` route -- so there is no CORS surface
to configure at all, and no second process to keep running for the
checkpoint to work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from citadel_platform._psql import PsqlError

from citadel_api import handlers
from citadel_api.deps import AuthError, load_app_state


def _web_dir() -> Path:
    # services/api/src/citadel_api/app.py -> repo root is four parents up,
    # the same derivation deps.py's _repo_root() uses. web/src is where the
    # UI actually lives -- see web/AGENTS.md's note on this checkpoint's
    # plain-HTML substitution for the originally-planned Vite+React build.
    return Path(__file__).resolve().parents[4] / "web" / "src"


async def _handle_auth_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AuthError)
    return JSONResponse({"error": exc.message}, status_code=401)


async def _handle_psql_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, PsqlError)
    # Never the raw psql stderr to a client -- it can echo back SQL text.
    # Printed for whoever is running the process to see; no logging
    # configuration exists yet at M0 (that is PLAN-M0 task 9, not this one).
    print(f"[citadel-api] database error: {exc}")
    return JSONResponse({"error": "database unavailable"}, status_code=503)


def create_app() -> Starlette:
    """Build one Starlette app: load `AppState` once (`deps.py`), register
    every route, and mount the web UI's static files at `/` last, so the UI
    can never shadow an `/api/...` route.
    """
    state = load_app_state()

    routes: list[Any] = [
        Route("/api/health", handlers.health, methods=["GET"]),
        Route("/api/registry/tools", handlers.list_registry_tools, methods=["GET"]),
        Route("/api/demo/users", handlers.list_demo_users, methods=["GET"]),
        Route("/api/auth/session", handlers.create_session, methods=["POST"]),
        Route("/api/me", handlers.whoami, methods=["GET"]),
        Route("/api/policy/try", handlers.try_policy, methods=["POST"]),
        Route("/api/audit/recent", handlers.recent_audit, methods=["GET"]),
        Route("/api/audit/verify", handlers.verify_audit, methods=["GET"]),
    ]

    web_dir = _web_dir()
    if web_dir.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=str(web_dir), html=True), name="web"))

    app = Starlette(
        routes=routes,
        exception_handlers={
            AuthError: _handle_auth_error,
            PsqlError: _handle_psql_error,
        },
    )
    app.state.citadel = state
    return app


__all__ = ["create_app"]
