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

from citadel_deliverables import (
    ArtifactError,
    TaskFacts,
    build_provenance,
    cited_ids,
    decide,
    generate,
    get_artifact,
    read_bytes,
    to_html,
)
from citadel_knowledge import evidence as resolve_evidence
from citadel_knowledge import task_evidence
from citadel_runtime import ACTIVE, LATEST_VERSION, after_decision, after_edit, get_task

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
    where = ("WHERE a.requires_approval AND a.status = 'VERIFIED' AND NOT EXISTS "
             f"(SELECT 1 FROM approvals p WHERE p.artifact_id = a.id) AND {LATEST_VERSION} " if awaiting else "")
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
                              approver=user, audit=app.audit, gateway=app.gateway,
                              artifact_title=str(artifact.get("title") or artifact.get("filename") or ""))
        return {"outcome": outcome, "task": task}

    try:
        return JSONResponse(await blocking(run))
    except ArtifactError as exc:
        return error(str(exc), 409)


def _editable(user: Any, artifact: dict[str, Any], task: dict[str, Any], newest: int) -> str | None:
    """Why this person may not edit this version now -- or None if they may."""
    if task["submitted_by"] != user.user_id:
        return "only the person who asked for this deliverable edits it; an approver approves or rejects"
    if not artifact.get("template_id") or not (artifact.get("provenance") or {}).get("render_inputs"):
        return "this file was not generated from a template, so there is nothing structured to edit"
    if int(artifact["version"]) != newest:
        return f"this is v{artifact['version']}; edit the newest version (v{newest})"
    if task["status"] in ACTIVE or task["status"] == "paused" or task.get("pause_requested"):
        return f"the agents are working on this task ({task['status']}); edit once they have finished"
    return None


async def artifact_content(request: Request) -> Response:
    """What the editor opens: the structured content a version was generated from, the
    template it fills, every version so far, and the evidence the task holds -- the ids
    a person may cite while editing."""
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)

    def gather() -> dict[str, Any]:
        task = get_task(app.db, str(artifact["task_id"])) or {}
        template = next((t for t in app.registry.templates if t.id == artifact.get("template_id")), None)
        versions = app.db.query(
            "SELECT a.id::text AS id, a.version, a.status, a.created_at, a.created_by, a.released_at, "
            "a.provenance -> 'render_inputs' ->> 'revision_note' AS note, "
            "jsonb_array_length(coalesce(a.verification -> 'flagged_claims', '[]')) AS flagged FROM artifacts a "
            "WHERE a.task_id = %(t)s::uuid AND a.template_id IS NOT DISTINCT FROM %(k)s ORDER BY a.version",
            {"t": artifact["task_id"], "k": artifact.get("template_id")},
        )
        newest = max([int(v["version"]) for v in versions] or [int(artifact["version"])])
        inputs = (artifact.get("provenance") or {}).get("render_inputs") or {}
        held = [
            {"evidence_id": e["evidence_id"], "kind": e["kind"], "title": e.get("title") or (e.get("detail") or {}).get("title"),
             "version": e.get("version"), "page": e.get("page"), "text": str(e.get("text") or "")[:900]}
            for e in task_evidence(app.db, str(artifact["task_id"]))
        ]
        return {
            "artifact": {k: artifact.get(k) for k in ("id", "task_id", "title", "template_id", "filename", "kind", "status",
                                                      "version", "requires_approval", "verification", "created_at")},
            "task": {k: task.get(k) for k in ("id", "title", "goal", "status", "classification", "submitted_by")},
            "template": None if template is None else {
                "id": template.id, "description": template.description, "approval_block": template.approval_block,
                "sections": [{"key": x.key, "type": x.type, "required": x.required, "cited": x.cited,
                              "min_items": x.min_items, "omit_when_empty": x.omit_when_empty}
                             for x in template.sections]},
            "content": inputs.get("content") or {},
            "versions": versions,
            "evidence": held,
            "editable": _editable(user, artifact, task, newest) is None,
            "why_not": _editable(user, artifact, task, newest),
        }

    return JSONResponse(await blocking(gather))


async def edit_artifact(request: Request) -> Response:
    """Save a person's edit as the next version: rendered into the same template,
    re-verified on all four tiers against the evidence the task holds, journalled.
    A citation the task was never given fails tier 3; a number no cited source states
    is flagged at tier 4 -- a person's edit is held to the agent's standard."""
    app, user, artifact = await _visible_artifact(request)
    if artifact is None:
        return error("no such artifact", 404)
    body = await json_body(request)
    content = body.get("content")
    if not isinstance(content, dict) or not content:
        return error("'content' must be the deliverable's sections, as an object")
    note = " ".join(str(body.get("note") or "").split())[:200]

    def save() -> dict[str, Any]:
        task = get_task(app.db, str(artifact["task_id"]))
        if task is None:
            raise ArtifactError("no such task")
        newest = int(app.db.scalar(
            "SELECT max(version) FROM artifacts WHERE task_id = %(t)s::uuid AND template_id IS NOT DISTINCT FROM %(k)s",
            {"t": artifact["task_id"], "k": artifact.get("template_id")}) or artifact["version"])
        refusal = _editable(user, artifact, task, newest)
        if refusal:
            raise PermissionError(refusal)
        template = next((t for t in app.registry.templates if t.id == artifact["template_id"]), None)
        if template is None:
            raise ArtifactError(f"template {artifact['template_id']!r} is no longer registered")
        held = resolve_evidence(app.db, task["id"], cited_ids(template, content))
        generated = generate(
            app.db, app.data_dir, app.registry_dir, template, content,
            task=TaskFacts(task["id"], str(task["classification"]).upper(), str(task["goal"])),
            author=user, evidence=held, audit=app.audit,
            revision_note=f"Edited by {user.username}" + (f": {note}" if note else ""),
        )
        updated = after_edit(app.db, task_id=task["id"], editor=user, artifact_id=generated.artifact_id,
                             version=generated.version, status=generated.status,
                             requires_approval=template.approval_block, note=note, audit=app.audit)
        return {"generated": generated.to_dict(), "task": {k: updated.get(k) for k in ("id", "status", "result")}}

    try:
        return JSONResponse(await blocking(save), status_code=201)
    except PermissionError as exc:
        return error(str(exc), 409)
    except ArtifactError as exc:
        return error(str(exc), 409)


__all__ = [
    "artifact_content",
    "edit_artifact",
    "artifacts_index",
    "artifact_detail",
    "artifact_download",
    "artifact_preview",
    "artifact_provenance",
    "decide_artifact",
]
