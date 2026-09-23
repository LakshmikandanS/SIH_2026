"""Artifacts: list, inspect, preview, download -- and the approval decision.

Downloads are served only after the stored bytes are re-hashed and matched against the
recorded SHA-256. The decision endpoint is the approver's: it records the decision in
the deliverables lifecycle (which re-renders, re-verifies, freezes and releases on
acceptance) and then moves the task on in the runtime (completion, or its one bounded
revision).
"""

from __future__ import annotations

import html
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from citadel_deliverables import ArtifactError, build_provenance, decide, get_artifact, read_bytes, to_html
from citadel_runtime import after_decision, get_task

from citadel_api.common import blocking, can_view_task, error, json_body, state
from citadel_api.deps import require_role, require_user

_LIST_SQL = (
    "SELECT a.id::text AS id, a.task_id::text AS task_id, a.title, a.template_id, a.filename, a.kind, a.status, "
    "a.version, a.classification, a.requires_approval, encode(a.sha256, 'hex') AS sha256, a.created_at, a.released_at, "
    "a.verification -> 'passed' AS passed, jsonb_array_length(coalesce(a.verification -> 'flagged_claims', '[]')) AS flagged, "
    "t.goal, t.status AS task_status, u.external_identity AS submitted_by, u.display_name AS submitted_by_name, "
    "t.classification AS task_classification, "
    "EXISTS (SELECT 1 FROM approvals p WHERE p.artifact_id = a.id) AS decided "
    "FROM artifacts a JOIN tasks t ON t.id = a.task_id JOIN users u ON u.id = t.submitted_by "
)


def _task_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"submitted_by": row["submitted_by"], "classification": row.get("task_classification") or row.get("classification")}


async def artifacts_index(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    awaiting = request.query_params.get("awaiting") == "1"
    where = "WHERE a.requires_approval AND a.status = 'VERIFIED' AND NOT EXISTS " \
            "(SELECT 1 FROM approvals p WHERE p.artifact_id = a.id) " if awaiting else ""
    rows = await blocking(app.db.query, _LIST_SQL + where + "ORDER BY a.created_at DESC LIMIT 200")
    return JSONResponse({"artifacts": [r for r in rows if can_view_task(user, _task_view(r))]})


async def _visible_artifact(request: Request) -> tuple[Any, Any, Any]:
    app = state(request)
    user = require_user(request, app)
    artifact = await blocking(get_artifact, app.db, request.path_params["artifact_id"])
    if artifact is None:
        return app, user, None
    task = await blocking(get_task, app.db, str(artifact["task_id"]))
    return app, user, artifact if can_view_task(user, task) else None


async def artifact_detail(request: Request) -> Response:
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    approvals = await blocking(
        app.db.query,
        "SELECT p.decision, p.reason, p.decided_at, u.external_identity AS approver, u.display_name "
        "FROM approvals p JOIN users u ON u.id = p.approver_id WHERE p.artifact_id = %(a)s::uuid ORDER BY p.decided_at",
        {"a": artifact["id"]},
    )
    return JSONResponse({"artifact": artifact, "approvals": approvals})


async def artifact_download(request: Request) -> Response:
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    try:
        row, data = await blocking(read_bytes, app.db, app.data_dir, artifact["id"])
    except ArtifactError as exc:
        return error(str(exc), 409)
    return Response(data, media_type=str(row.get("mime_type") or "application/octet-stream"), headers={
        "Content-Disposition": f'attachment; filename="{row["filename"]}"',
        "X-Citadel-SHA256": str(row["sha256"]),
        "X-Citadel-Classification": str(row["classification"]).upper(),
    })


async def artifact_preview(request: Request) -> Response:
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    try:
        row, data = await blocking(read_bytes, app.db, app.data_dir, artifact["id"])
    except ArtifactError as exc:
        return error(str(exc), 409)
    kind = str(row.get("kind") or "")
    if kind in ("docx", "xlsx"):
        body = await blocking(to_html, kind, data)
    else:
        body = f"<pre>{html.escape(data.decode('utf-8', 'replace')[:200_000])}</pre>"
    return HTMLResponse(body)


async def artifact_provenance(request: Request) -> Response:
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    record = (artifact.get("provenance") or {}).get("record") or await blocking(build_provenance, app.db, artifact["id"])
    return JSONResponse(record, headers={"Content-Disposition": f'inline; filename="provenance-{artifact["id"]}.json"'})


async def decide_artifact(request: Request) -> Response:
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    require_role(user, "approver")
    body = await json_body(request)
    approve = body.get("approve")
    if not isinstance(approve, bool):
        return error("'approve' must be true or false")
    comment = str(body.get("comment") or "").strip()
    if not approve and not comment:
        return error("a rejection needs a comment -- it is what the revision works from")

    def run() -> dict[str, Any]:
        outcome = decide(app.db, app.data_dir, app.registry_dir, list(app.registry.templates),
                         artifact_id=artifact["id"], approver=user, approve=approve, comment=comment, audit=app.audit)
        task = after_decision(app.db, task_id=str(artifact["task_id"]), approved=approve, comment=comment,
                              approver=user, audit=app.audit)
        return {"outcome": outcome, "task": task}

    try:
        return JSONResponse(await blocking(run))
    except ArtifactError as exc:
        return error(str(exc), 409)


__all__ = [
    "artifacts_index",
    "artifact_detail",
    "artifact_download",
    "artifact_preview",
    "artifact_provenance",
    "decide_artifact",
]
