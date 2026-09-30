"""The trusted workflow zone's single FastAPI process (design doc §2).

    "CLI-facing API - Query Router - Orchestrator - Control Plane (Identity,
     Policy, Capability, Approval) - Model Router - Data Plane [...]
     Consolidating them into one FastAPI process is an explicit MVP
     simplification for build speed."

Created by step 4 because §6.4's `/login` and §6.8's
`/admin/tools/{tool_name}/disable` are the first endpoints in the system. It is
deliberately a thin assembly point: **later steps add a router import and one
`include_router` line here**, and nothing else.

Endpoints mounted so far
------------------------
  POST /login                              §6.4  Identity
  POST /admin/tools/{tool_name}/disable    §6.8  Emergency control (admin only)
  POST /task                               §6.2  Query Router role (step 7)
  POST /internal/orchestrate               §6.2  Canonical handoff (step 7)
  GET  /tasks/{id}                         §6.2  Orchestrator, sole owner (step 7)
  GET  /tasks/{id}/trace                   §6.2  Orchestrator, sole owner (step 7)
  POST /approvals/{approval_id}/decision   §6.10 The one transactional endpoint (step 8)

The Tool Gateway is intentionally NOT an HTTP endpoint. §2 puts the agent loop
and the gateway in this same process and §6.6 defines the hop as an in-process
call; exposing it over HTTP would create a second, unauthenticated route to
every tool.

Startup registers the three real tool backends (`rag.search`,
`python.execute`, `generate_report`) and ingests the demo RAG corpus, exactly
once, the same way `tests/demos/step5_execution.py`/`step6_rag.py` do it by
hand for their own throwaway processes (§6.6 Step C, §6.9).
"""

from __future__ import annotations

from fastapi import FastAPI

from app.approval.router import router as approval_router
from app.artifact.router import router as artifact_router
from app.identity.router import router as identity_router
from app.orchestrator.router import router as orchestrator_router
from app.orchestrator.startup import configure as configure_orchestrator
from app.policy.router import router as admin_router
from app.rag.router import router as documents_router
from app.ui.router import router as ui_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Citadel — Sovereign Agentic AI Workbench (MVP vertical slice)",
        version="0.8.0",
        description=(
            "Trusted workflow zone. Every endpoint derives the acting identity "
            "from the verified session JWT (§6.4); a client-supplied user_id or "
            "approver_id in a request body is ignored."
        ),
    )
    app.include_router(identity_router)
    app.include_router(admin_router)
    app.include_router(orchestrator_router)
    app.include_router(approval_router)
    app.include_router(artifact_router)
    app.include_router(documents_router)
    app.include_router(ui_router)

    @app.on_event("startup")
    def _startup() -> None:  # pragma: no cover -- exercised by running the app for real
        configure_orchestrator()

    return app


app = create_app()


# --- Runnable entrypoint (§9, the CLI's server) --------------------------------
# `.venv/Scripts/python -m app.main` starts the real trusted-zone process on a
# fixed local port (`app.config.SERVER_HOST`/`SERVER_PORT`) so `cli/` has a real
# HTTP server to talk to -- going through `create_app()` (not a hand-rolled,
# smaller app) so the startup hook above still registers the three tool
# backends and ingests the demo RAG corpus exactly as it does for every other
# real run of this process.
#
# Demo-user seeding lives here, deliberately NOT inside `_startup()` above:
# every test in this repo that uses `TestClient(create_app())` as a context
# manager also fires that FastAPI startup event, and several of them create
# their own "j.rao"/"a.singh"/"s.mehta" rows by hand immediately beforehand
# (see e.g. tests/demos/step7_orchestrator.py, step8_artifact.py). Seeding
# there too would either collide with `User.username`'s unique constraint or
# depend on fixture-ordering luck. Putting it only behind `__main__` means it
# runs exactly once, for a human actually starting the server, and never
# during the test suite.
_DEMO_USERS = (
    # username,   password,        roles,                    clearance,      department
    ("j.rao", "engineer-pw", ("engineer",), "CONFIDENTIAL", "maintenance"),
    ("a.singh", "approver-pw", ("approver",), "CONFIDENTIAL", "maintenance"),
    ("s.mehta", "admin-pw", ("admin",), "CONFIDENTIAL", "security"),
)


def _seed_demo_users() -> None:
    """Idempotently create the three §1.1/§1.2/§1.3 personas so a human can
    `citadel login` immediately after starting the server. Skips any username
    that already exists (a restart against the same `var/citadel.db` must not
    error, and must not touch a user someone has since modified)."""
    from app.db import init_db
    from app.db.engine import SessionLocal
    from app.identity import create_user, get_user_by_username

    init_db.create_all()
    with SessionLocal() as session:
        created = []
        for username, password, roles, clearance, department in _DEMO_USERS:
            if get_user_by_username(session, username) is not None:
                continue
            create_user(
                session,
                username=username,
                password=password,
                roles=list(roles),
                clearance=clearance,
                department=department,
            )
            created.append(username)
        session.commit()
    if created:
        print(f"Seeded demo users: {', '.join(created)}")


def run_server() -> None:  # pragma: no cover -- exercised by running the app for real
    """Seed the demo personas, then serve `app` for real. Called by this
    module's own `__main__` guard and by `citadel serve` (`cli/main.py`) --
    the one place both entrypoints share, so there are not two slightly
    different ways to start the same process."""
    import uvicorn

    from app import config

    _seed_demo_users()
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)


if __name__ == "__main__":  # pragma: no cover -- exercised by running the app for real
    run_server()
