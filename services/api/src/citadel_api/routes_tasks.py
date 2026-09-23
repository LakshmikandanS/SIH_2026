"""Tasks: submit, list, read, stream, cancel, probe, and the task's own files.

Submission inserts a row and returns at once -- the agent runs in the worker service,
never inside this request. Progress reaches the browser as Server-Sent Events read from
the task's durable journal, so a page reload, a second viewer or an API restart all
see the same steps in the same order.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

from citadel_knowledge import task_evidence
from citadel_platform.storage import PathEscape, scoped_path
from citadel_runtime import TERMINAL, Journal, TaskError, get_task, list_tasks, request_cancel, submit
from citadel_sovereignty import EgressRecorder, run_probe, task_report

from citadel_api.common import blocking, can_view_task, error, json_body, state
from citadel_api.deps import require_user

#: The stream closes once the task has nothing more to say for now.
_QUIET = TERMINAL | {"awaiting_approval"}


async def create_task(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    for claimed in ("user_id", "submitted_by", "actor", "user"):
        body.pop(claimed, None)  # identity comes from the session token, never the body
    try:
        task = await blocking(
            submit, app.db, user=user, goal=str(body.get("goal") or ""),
            classification=body.get("classification") or None,
            profile_ceiling=app.registry.profile.classification_ceiling,
            requirements={k: body[k] for k in ("template_id",) if k in body},
            audit=app.audit,
        )
    except TaskError as exc:
        return error(str(exc))
    return JSONResponse(task, status_code=201)


async def tasks_index(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    scope = request.query_params.get("scope", "mine")
    rows = await blocking(list_tasks, app.db, user_id=None if scope == "all" else user.user_id, limit=100)
    return JSONResponse({"tasks": [t for t in rows if can_view_task(user, t)]})


async def _visible_task(request: Request) -> tuple[Any, Any, Any]:
    app = state(request)
    user = require_user(request, app)
    task = await blocking(get_task, app.db, request.path_params["task_id"])
    if not can_view_task(user, task):
        return app, user, None
    return app, user, task


async def task_detail(request: Request) -> Response:
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)

    def gather() -> dict[str, Any]:
        journal = Journal(app.db, task["id"]).entries(limit=2000)
        artifacts = app.db.query(
            "SELECT id::text AS id, title, template_id, filename, kind, status, version, requires_approval, "
            "encode(sha256, 'hex') AS sha256, verification, created_at, released_at FROM artifacts "
            "WHERE task_id = %(t)s::uuid ORDER BY created_at",
            {"t": task["id"]},
        )
        return {"task": task, "journal": journal, "artifacts": artifacts, "evidence": task_evidence(app.db, task["id"])}

    return JSONResponse(await blocking(gather))


async def task_events(request: Request) -> Response:
    """SSE: every journal entry after `after`, then new ones as they are written."""
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        after = 0
    journal = Journal(app.db, task["id"])

    async def stream() -> AsyncIterator[bytes]:
        nonlocal after
        idle = 0
        quiet_polls = 0
        yield b"retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            entries = await blocking(journal.entries, after)
            for entry in entries:
                after = int(entry["step_seq"])
                data = json.dumps(entry, default=str)
                yield f"id: {after}\nevent: step\ndata: {data}\n\n".encode("utf-8")
            if entries:
                idle = 0
            else:
                idle += 1
                if idle % 25 == 0:
                    yield b": keep-alive\n\n"
            current = await blocking(get_task, app.db, task["id"])
            status = (current or {}).get("status")
            if status in _QUIET and not entries:
                quiet_polls += 1
                if quiet_polls >= 2:
                    yield f"event: end\ndata: {json.dumps({'status': status})}\n\n".encode("utf-8")
                    return
            else:
                quiet_polls = 0
            await asyncio.sleep(0.6)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def cancel_task(request: Request) -> Response:
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)
    if task["submitted_by"] != user.user_id and "admin" not in user.roles:
        return error("only the person who submitted a task (or an admin) may cancel it", 403)
    try:
        updated = await blocking(request_cancel, app.db, task["id"], user=user, audit=app.audit)
    except TaskError as exc:
        return error(str(exc))
    return JSONResponse(updated)


async def probe_task(request: Request) -> Response:
    """The deliberate probe, attributed to a running task: the API process and the
    sandbox each try to reach the internet; the outcomes join the task's journal and
    egress record -- and the task carries on."""
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)
    report = await run_deliberate_probe(request, task_id=task["id"], actor_id=user.user_id)
    await blocking(Journal(app.db, task["id"]).append, "probe", {"by": user.user_id, **report})
    return JSONResponse(report)


async def run_deliberate_probe(request: Request, *, task_id: str | None, actor_id: str) -> dict[str, Any]:
    app = state(request)
    recorder = app.sovereignty.recorder if app.sovereignty else EgressRecorder(app.db, "api", audit=app.audit)
    api_report = await blocking(run_probe, recorder, task_id=task_id, agent_id="probe")
    sandbox_report: dict[str, Any]
    if app.sandbox is not None:
        sandbox_report = await blocking(app.sandbox.probe, task_id=task_id)
        sandbox_recorder = EgressRecorder(app.db, "sandbox", audit=app.audit)
        for result in sandbox_report.get("results") or []:
            host, _, port = str(result.get("target", "")).rpartition(":")
            kind = {"connected": "observed"}.get(str(result.get("outcome")), str(result.get("outcome") or "blocked"))
            sandbox_recorder.record(kind if kind in ("blocked", "dns_denied", "observed") else "blocked", "probe", host,
                                    int(port) if port.isdigit() else None,
                                    {"outcome": result.get("outcome"), "error": result.get("error"), "deliberate": True},
                                    task_id=task_id, agent_id="probe")
        await blocking(sandbox_recorder.flush)
    else:
        sandbox_report = {"process": "sandbox", "note": "no sandbox service on this deployment (process sandbox in the worker)"}
    await blocking(recorder.flush)
    await blocking(app.audit.record, "sovereignty.probe", actor_id=actor_id, payload={
        "task_id": task_id,
        "api_blocked": api_report.get("egress_blocked"),
        "sandbox_blocked": sandbox_report.get("egress_blocked"),
    })
    return {"api": api_report, "sandbox": sandbox_report,
            "egress_blocked": bool(api_report.get("egress_blocked")) and sandbox_report.get("egress_blocked") is not False}


async def task_sovereignty(request: Request) -> Response:
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)
    return JSONResponse(await blocking(task_report, app.db, task["id"]))


async def task_workspace(request: Request) -> Response:
    app, user, task = await _visible_task(request)
    if task is None:
        return error("no such task", 404)
    root = app.data_dir.workspace(task["id"])
    relative = request.path_params.get("path") or ""
    if relative:
        try:
            path = scoped_path(root, relative)
        except PathEscape:
            return error("path outside the task workspace", 400)
        if not path.is_file():
            return error("no such file", 404)
        return FileResponse(str(path), filename=path.name)
    files = [
        {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size}
        for p in sorted(root.rglob("*")) if p.is_file()
    ]
    return JSONResponse({"files": files})


__all__ = [
    "create_task",
    "tasks_index",
    "task_detail",
    "task_events",
    "cancel_task",
    "probe_task",
    "run_deliberate_probe",
    "task_sovereignty",
    "task_workspace",
]
