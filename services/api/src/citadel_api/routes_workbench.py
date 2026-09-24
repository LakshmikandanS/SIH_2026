"""The workbench: where people and agents work on the same tasks.

Everything a person does here goes through the doors an agent uses -- a tool runs through
the policy chokepoint with a signed receipt, a note goes into the task's shared state,
pause and resume go through the task row -- and is journalled with who did it
(`human:<identity>`), so every agent sees it at its next step and the journal still
reconstructs the task. Identity always comes from the verified session.

Also here: the editor's task drafts (/task opens one; committing it submits the task),
the activity feed, which tools are running, available and recently used, and the
environment and sandbox state the right-hand panel shows -- for one task or for all the
work a person may see.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from citadel_contracts.domain import User
from citadel_knowledge import task_evidence
from citadel_runtime import (
    TERMINAL,
    BudgetLimits,
    Journal,
    TaskError,
    get_task,
    list_tasks,
    request_pause,
    request_resume,
    request_revision,
    submit,
)
from citadel_tools import ToolContext, actor_facts_for_task

from citadel_api.common import blocking, can_view_task, error, json_body, state
from citadel_api.deps import AppState, require_user

NOTE_KINDS = ("fact", "decision", "assumption", "question", "note")
_EVIDENCE = re.compile(r"^[EC]\d+$")
_DRAFT_NAME = re.compile(r"^[\w .,:()'/-]{1,90}$")
#: Journal entries worth a line in the activity feed.
_ACTIVITY_KINDS = (
    "submitted", "claimed", "recalled", "planned", "replanned", "agents", "agent", "tool_call", "tool_result",
    "paused", "pause_requested", "resumed", "steered", "human", "memory", "revision", "revision_requested",
    "decision", "finished", "failed", "cancelled", "cancel_requested", "probe",
)
_SEARCH_LABELS = {"web.search": "web search", "docs.search": "retrieval DB query", "memory.recall": "memory recall",
                  "docs.diff": "document diff", "docs.read": "document read", "code.run": "sandbox run",
                  "calc.evaluate": "calculation", "state.note": "shared-state note", "fs.write": "editor save",
                  "fs.read": "editor open", "doc.generate": "document generation", "vision.extract": "vision read",
                  "sheet.read": "sheet read", "sheet.write": "sheet write", "workbench.inspect": "workbench lookup"}


# -- who may do what -----------------------------------------------------------------------------


def _owner_or_admin(user: User, task: Mapping[str, Any]) -> bool:
    return task.get("submitted_by") == user.user_id or "admin" in user.roles


async def _task_for(request: Request) -> tuple[AppState, User, Optional[dict[str, Any]]]:
    app = state(request)
    user = require_user(request, app)
    task = await blocking(get_task, app.db, request.path_params["task_id"])
    return app, user, task if can_view_task(user, task) else None


def _visible_tasks(app: AppState, user: User, *, limit: int = 60) -> list[dict[str, Any]]:
    reviewer = bool({"approver", "admin"} & set(user.roles))
    rows = list_tasks(app.db, user_id=None if reviewer else user.user_id, limit=limit)
    return [t for t in rows if can_view_task(user, t)]


def _names(app: AppState, task_ids: Sequence[str]) -> dict[tuple[str, str], str]:
    """(task id, agent id) -> the name people see: Agent 1, Lead agent, a person's name."""
    names: dict[tuple[str, str], str] = {}
    if task_ids:
        for row in app.db.query("SELECT task_id::text AS task_id, agent_id, name FROM task_agents "
                                "WHERE task_id = ANY(%(ids)s::uuid[])", {"ids": list(task_ids)}):
            names[(str(row["task_id"]), str(row["agent_id"]))] = str(row["name"])
    return names


def _who(agent_id: Optional[str], task_id: str, names: Mapping[tuple[str, str], str],
         people: Mapping[str, str]) -> str:
    if not agent_id:
        return "Task"
    if agent_id.startswith("human:"):
        identity = agent_id.removeprefix("human:")
        return people.get(identity, identity)
    return names.get((task_id, agent_id)) or ("Lead agent" if agent_id == "lead" else agent_id)


def _author(row: Mapping[str, Any]) -> str:
    """Who wrote a shared-state entry, as an id _who understands: the agent's own id, or
    `human:<identity>` for a person."""
    author = str(row["author"])
    return author if author.startswith("human:") else str(row.get("agent_id") or author)


def _people(app: AppState) -> dict[str, str]:
    return {str(r["external_identity"]): str(r["display_name"])
            for r in app.db.query("SELECT external_identity, display_name FROM users")}


# -- pause, resume, notes, a person's tool run ------------------------------------------------------


async def pause_task(request: Request) -> Response:
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    if not _owner_or_admin(user, task):
        return error("only the person who submitted a task (or an admin) may pause it", 403)
    return JSONResponse(await blocking(request_pause, app.db, task["id"], user=user, audit=app.audit))


async def resume_task(request: Request) -> Response:
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    if not _owner_or_admin(user, task):
        return error("only the person who submitted a task (or an admin) may resume it", 403)
    return JSONResponse(await blocking(request_resume, app.db, task["id"], user=user, audit=app.audit))


async def revise_task(request: Request) -> Response:
    """/revise: send the deliverable back to the agents with an instruction. They start
    from its current version, hand edits included, and write the next one."""
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    body = await json_body(request)
    try:
        updated = await blocking(request_revision, app.db, task["id"], user=user,
                                 instruction=str(body.get("instruction") or ""), audit=app.audit)
    except TaskError as exc:
        return error(str(exc), 409)
    return JSONResponse(updated)


async def add_note(request: Request) -> Response:
    """A person adds to the task's shared state: a fact (citing evidence the task
    holds), a decision, an assumption, a question -- or a note to one agent, which that
    agent is told at its next step (/steer)."""
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    if task["status"] in TERMINAL:
        return error(f"the task is {task['status']}; its shared state is closed")
    body = await json_body(request)
    kind = str(body.get("kind") or "note")
    content = " ".join(str(body.get("content") or "").split())
    if kind not in NOTE_KINDS:
        return error(f"kind must be one of {', '.join(NOTE_KINDS)}")
    if not 3 <= len(content) <= 2000:
        return error("a note is between 3 and 2000 characters")
    evidence = [str(e).strip().upper() for e in body.get("evidence") or [] if _EVIDENCE.match(str(e).strip().upper())]
    to = str(body.get("to") or "").strip() or None

    def write() -> dict[str, Any]:
        if evidence:
            held = {str(r["evidence_id"]) for r in app.db.query(
                "SELECT evidence_id FROM task_evidence WHERE task_id = %(t)s::uuid AND evidence_id = ANY(%(ids)s)",
                {"t": task["id"], "ids": evidence})}
            unknown = [e for e in evidence if e not in held]
            if unknown:
                raise TaskError(f"{', '.join(unknown)} is not evidence this task holds")
        if to is not None:
            known = {str(r["agent_id"]) for r in app.db.query(
                "SELECT agent_id FROM task_agents WHERE task_id = %(t)s::uuid", {"t": task["id"]})} | {"lead"}
            if to not in known:
                raise TaskError(f"no agent {to!r} on this task (agents: {', '.join(sorted(known))})")
        row = app.db.query_one(
            "INSERT INTO task_shared_state (task_id, kind, content, evidence, agent_id, author, addressed_to) VALUES "
            "(%(t)s::uuid, %(k)s, %(c)s, %(e)s, NULL, %(by)s, %(to)s) RETURNING id, created_at",
            {"t": task["id"], "k": kind, "c": content, "e": evidence, "by": f"human:{user.user_id}", "to": to},
        )
        Journal(app.db, task["id"], agent_id=f"human:{user.user_id}").append("human", {
            "by": user.user_id, "name": user.username, "action": "steer" if to else "note", "kind": kind, "to": to,
            "summary": content[:300], "evidence": evidence,
        })
        if app.audit is not None:
            app.audit.record("task.note_added", actor_id=user.user_id,
                             payload={"task_id": task["id"], "kind": kind, "to": to, "chars": len(content)})
        return {"id": row["id"] if row else None, "kind": kind, "to": to, "content": content, "evidence": evidence}

    try:
        return JSONResponse(await blocking(write), status_code=201)
    except TaskError as exc:
        return error(str(exc))


async def run_tool(request: Request) -> Response:
    """A person runs a tool on their own task -- through the chokepoint, with a receipt,
    as themselves (their clearance capped at the task's classification). The result is
    journalled like an agent's, so the agents take it as known at their next step."""
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    if task["submitted_by"] != user.user_id:
        # Anyone else's reads would enter a task whose owner may not be cleared for them.
        return error("only the person who submitted a task may run tools on it", 403)
    if task["status"] in TERMINAL:
        return error(f"the task is {task['status']}; start a new task to keep working")
    if app.chokepoint is None or app.boundary is None:
        return error("this deployment has no receipt keys, so tools cannot run here", 503)
    name = request.path_params["tool"]
    body = await json_body(request)
    arguments = body.get("arguments")
    if not isinstance(arguments, dict):
        return error("'arguments' must be an object")
    chokepoint, boundary = app.chokepoint, app.boundary
    actor = f"human:{user.user_id}"

    def run() -> dict[str, Any]:
        journal = Journal(app.db, task["id"], agent_id=actor)
        classification = str(task["classification"]).upper()
        ctx = ToolContext(
            task_id=task["id"], agent_id=actor, user=user,
            actor=actor_facts_for_task(user, app.registry, classification), task_classification=classification,
            db=app.db, data_dir=app.data_dir, registry=app.registry, registry_dir=app.registry_dir,
            boundary=boundary, gateway=app.gateway, audit=app.audit, tracer=app.tracer, sandbox=app.sandbox,
            goal=str(task["goal"]),
        )
        journal.append("tool_call", {"tool": name, "arguments": arguments, "by": user.user_id, "by_name": user.username})
        result = chokepoint.invoke(ctx, name, arguments)
        record = result.to_dict()
        record["for_model"] = result.for_model()
        record["arguments"] = arguments
        record["by"], record["by_name"] = user.user_id, user.username
        journal.append("tool_result", _bounded(record))
        if app.audit is not None:
            app.audit.record("task.human_step", actor_id=user.user_id,
                             payload={"task_id": task["id"], "tool": name, "status": result.status})
        return record

    return JSONResponse(await blocking(run))


def _bounded(record: dict[str, Any], limit: int = 30000) -> dict[str, Any]:
    for key in ("output", "detail", "for_model"):
        text = json.dumps(record.get(key), default=str, ensure_ascii=False)
        if len(text) > limit:
            record[key] = {"truncated": True, "preview": text[:limit]}
    return record


# -- drafts: the editor's task files ---------------------------------------------------------------------


_DRAFT_COLUMNS = ("id::text AS id, name, body, classification, committed_task_id::text AS committed_task_id, "
                  "created_at, updated_at")


async def drafts_index(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    rows = await blocking(app.db.query, f"SELECT {_DRAFT_COLUMNS} FROM task_drafts WHERE owner = %(o)s "
                                        "ORDER BY updated_at DESC LIMIT 100", {"o": user.user_id})
    return JSONResponse({"drafts": rows})


def _draft_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "name" in body:
        name = " ".join(str(body.get("name") or "").split())
        if not _DRAFT_NAME.match(name):
            raise TaskError("a draft's name is 1-90 plain characters")
        fields["name"] = name
    if "body" in body:
        text = str(body.get("body") or "")
        if len(text) > 4000:
            raise TaskError(f"the task text is {len(text)} characters; the limit is 4000")
        fields["body"] = text
    if "classification" in body:
        level = str(body.get("classification") or "").strip().lower() or None
        if level not in (None, "public", "internal", "confidential"):
            raise TaskError(f"unknown classification {level!r}")
        fields["classification"] = level
    return fields


async def create_draft(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    try:
        fields = _draft_fields({**body, "name": body.get("name") or "untitled task"})
    except TaskError as exc:
        return error(str(exc))
    row = await blocking(
        app.db.query_one,
        f"INSERT INTO task_drafts (owner, name, body, classification) VALUES (%(o)s, %(n)s, %(b)s, %(c)s) "
        f"RETURNING {_DRAFT_COLUMNS}",
        {"o": user.user_id, "n": fields["name"], "b": fields.get("body", ""), "c": fields.get("classification")},
    )
    return JSONResponse(row, status_code=201)


async def update_draft(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    try:
        fields = _draft_fields(body)
    except TaskError as exc:
        return error(str(exc))
    if not fields:
        return error("nothing to change: send name, body or classification")
    assignments = ", ".join(f"{key} = %({key})s" for key in fields)
    row = await blocking(
        app.db.query_one,
        f"UPDATE task_drafts SET {assignments} WHERE id = %(id)s::uuid AND owner = %(o)s RETURNING {_DRAFT_COLUMNS}",
        {**fields, "id": request.path_params["draft_id"], "o": user.user_id},
    )
    return JSONResponse(row) if row else error("no such draft", 404)


async def delete_draft(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    rows = await blocking(app.db.query, "DELETE FROM task_drafts WHERE id = %(id)s::uuid AND owner = %(o)s "
                                        "AND committed_task_id IS NULL RETURNING id::text AS id",
                          {"id": request.path_params["draft_id"], "o": user.user_id})
    return JSONResponse({"deleted": [r["id"] for r in rows]}) if rows else error(
        "no such uncommitted draft (a committed draft stays, as the record of what was asked)", 404)


async def commit_draft(request: Request) -> Response:
    """The editor's commit button: the draft's text becomes the task's goal, verbatim."""
    app = state(request)
    user = require_user(request, app)

    def commit() -> dict[str, Any]:
        draft = app.db.query_one(f"SELECT {_DRAFT_COLUMNS} FROM task_drafts WHERE id = %(id)s::uuid AND owner = %(o)s",
                                 {"id": request.path_params["draft_id"], "o": user.user_id})
        if draft is None:
            raise LookupError("no such draft")
        if draft["committed_task_id"]:
            raise TaskError("this draft was already committed; duplicate it to run it again")
        task = submit(app.db, user=user, goal=str(draft["body"] or ""), classification=draft["classification"],
                      profile_ceiling=app.registry.profile.classification_ceiling, audit=app.audit,
                      title=str(draft["name"]), draft_id=str(draft["id"]))
        app.db.execute("UPDATE task_drafts SET committed_task_id = %(t)s::uuid WHERE id = %(id)s::uuid",
                       {"t": task["id"], "id": draft["id"]})
        if app.audit is not None:
            app.audit.record("task.draft_committed", actor_id=user.user_id,
                             payload={"task_id": task["id"], "draft_id": draft["id"]})
        return task

    try:
        return JSONResponse(await blocking(commit), status_code=201)
    except LookupError as exc:
        return error(str(exc), 404)
    except TaskError as exc:
        return error(str(exc))


# -- the activity feed ------------------------------------------------------------------------------------


def _args_brief(arguments: Any) -> str:
    if not isinstance(arguments, Mapping):
        return ""
    content = arguments.get("content")
    if isinstance(content, Mapping):
        # A deliverable: what it is and the sections it has, never the whole draft.
        headings = [str(s.get("heading") or s.get("title") or "") for value in content.values()
                    if isinstance(value, list) for s in value if isinstance(s, Mapping)]
        headings = [h for h in headings if h]
        title = " ".join(str(content.get("title") or "").split())[:70]
        parts = [str(arguments.get("template_id") or "")] + ([f"'{title}'"] if title else [])
        if headings:
            parts.append("sections: " + ", ".join(headings[:6]) + (" …" if len(headings) > 6 else ""))
        return " ".join(p for p in parts if p)
    for key in ("query", "expression", "path", "task", "content", "document_id"):
        if arguments.get(key):
            return f"'{' '.join(str(arguments[key]).split())[:90]}'"
    if arguments.get("template_id"):
        return str(arguments["template_id"])
    return ""


def activity_line(entry: Mapping[str, Any], who: str) -> tuple[str, str]:
    """One journal entry as a person reads it in the feed, and how loudly: info, ok,
    warn or alert. Driven by the entry's type and payload, never by a tool's name except
    for the friendly label."""
    kind = str(entry["step_type"])
    p = entry.get("payload") or {}
    if kind == "submitted":
        return f"{p.get('name') or who} submitted {'a question' if p.get('kind') == 'ask' else 'a task'}", "info"
    if kind == "claimed":
        return ("Work resumed" if p.get("resumed") else "Work started") + f" ({len(p.get('tools') or [])} tools)", "info"
    if kind == "recalled":
        return f"{who} recalled {p.get('count', 0)} memory item(s)", "info"
    if kind in ("planned", "replanned"):
        steps = (p.get("plan") or {}).get("steps") or []
        return f"{who} {'re' if kind == 'replanned' else ''}planned {len(steps)} step(s)", "info"
    if kind == "agents":
        agents = p.get("agents") or []
        return "Work split across " + ", ".join(str(a.get("name")) for a in agents), "info"
    if kind == "agent":
        event = str(p.get("event") or "")
        level = {"failed": "warn", "done": "ok"}.get(event, "info")
        verb = {"done": "finished", "started": "started", "resumed": "resumed", "failed": "failed"}.get(event, event)
        return f"{p.get('name') or who} {verb}" + (f": {str(p.get('error'))[:120]}" if p.get("error") else ""), level
    if kind == "tool_call":
        tool = str(p.get("tool") or "")
        return f"{who}: {_SEARCH_LABELS.get(tool, tool)} invoked {_args_brief(p.get('arguments'))}".rstrip(), "info"
    if kind == "tool_result":
        tool = str(p.get("tool") or "")
        status = str(p.get("status") or "")
        summary = str(p.get("summary") or "")
        if tool == "state.note" and "question" in summary[:20]:
            return f"{who} raised a question: {summary.split(':', 1)[-1].strip()[:160]}", "warn"
        level = "ok" if status == "ok" else "warn" if status in ("denied", "not_found", "invalid") else "alert"
        return f"{who}: {summary[:180]}", level
    if kind in ("paused", "pause_requested"):
        return ("Paused" if kind == "paused" else "Pause requested") + (f" by {p.get('by')}" if p.get("by") else ""), "warn"
    if kind == "resumed":
        return "Resumed" + (f" by {p.get('by')}" if p.get("by") and p.get("by") != "worker" else ""), "info"
    if kind == "steered":
        return f"{who} was told by {str(p.get('by') or '').removeprefix('human:')}: {str(p.get('text'))[:120]}", "info"
    if kind == "human":
        name = p.get("name") or who
        if p.get("action") == "edited":
            return (f"{name} edited the deliverable (v{p.get('version')}, {str(p.get('status') or '').lower()}): "
                    f"{str(p.get('summary'))[:120]}", "ok" if p.get("status") == "VERIFIED" else "warn")
        if p.get("to"):
            return f"{name} told {p.get('to')}: {str(p.get('summary'))[:140]}", "info"
        return f"{name} added a {p.get('kind')}: {str(p.get('summary'))[:140]}", "info"
    if kind == "memory":
        return (f"Memory manager: {p.get('summary') or 'nothing new'}" if not p.get("error")
                else f"Memory manager could not record: {str(p.get('error'))[:120]}"), "info"
    if kind == "revision_requested":
        if p.get("kind") == "owner":
            return f"{p.get('name') or who} asked for a revision of v{p.get('from_version')}: {str(p.get('comment'))[:140]}", "warn"
        return f"Revision requested after review: {str(p.get('comment'))[:140]}", "warn"
    if kind == "revision":
        start = f" from v{p.get('from_version')}" if p.get("from_version") else ""
        return f"{who} is revising the deliverable{start}: {str(p.get('comment'))[:120]}", "info"
    if kind == "decision":
        return f"{p.get('name') or 'Approver'} {'approved' if p.get('approved') else 'rejected'} the deliverable", (
            "ok" if p.get("approved") else "warn")
    if kind in ("finished", "failed", "cancelled", "cancel_requested"):
        status = str(p.get("status") or kind)
        level = {"completed": "ok", "awaiting_approval": "ok", "failed": "alert", "cancelled": "warn"}.get(status, "info")
        text = {"awaiting_approval": "Finished: awaiting approval", "completed": "Finished"}.get(status, status.replace("_", " "))
        return (text + (f": {str(p.get('error'))[:140]}" if p.get("error") else "")).capitalize(), level
    if kind == "probe":
        return "Deliberate egress probe run: " + ("blocked" if p.get("egress_blocked") else "NOT blocked"), (
            "ok" if p.get("egress_blocked") else "alert")
    return kind.replace("_", " "), "info"


def gather_activity(app: AppState, user: User, *, after: int = 0, limit: int = 80,
                    task_id: Optional[str] = None) -> dict[str, Any]:
    tasks = {t["id"]: t for t in _visible_tasks(app, user)}
    ids = [task_id] if task_id and task_id in tasks else list(tasks)
    if not ids:
        return {"items": [], "cursor": after}
    rows = app.db.query(
        "SELECT id, task_id::text AS task_id, step_seq, step_type, payload, agent_id, created_at FROM task_journal "
        "WHERE task_id = ANY(%(ids)s::uuid[]) AND id > %(after)s AND step_type = ANY(%(kinds)s) "
        "ORDER BY id DESC LIMIT %(n)s",
        {"ids": ids, "after": after, "kinds": list(_ACTIVITY_KINDS), "n": limit},
    )
    names, people = _names(app, ids), _people(app)
    items = []
    for row in reversed(rows):
        who = _who(row.get("agent_id"), row["task_id"], names, people)
        text, level = activity_line(row, who)
        task = tasks[row["task_id"]]
        items.append({"id": row["id"], "at": row["created_at"], "task_id": row["task_id"],
                      "task": task.get("title") or str(task.get("goal"))[:60], "kind": row["step_type"],
                      "agent_id": row.get("agent_id"), "who": who, "text": text, "level": level})
    return {"items": items, "cursor": max([after, *[int(r["id"]) for r in rows]])}


async def activity(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    try:
        after = int(request.query_params.get("after", "0"))
        limit = max(1, min(int(request.query_params.get("limit", "80")), 300))
    except ValueError:
        return error("after and limit are numbers")
    return JSONResponse(await blocking(gather_activity, app, user, after=after, limit=limit,
                                       task_id=request.query_params.get("task_id")))


# -- tools: available, running now, recently used --------------------------------------------------------


def gather_tools(app: AppState, user: User) -> dict[str, Any]:
    classification = user.clearance.upper()
    offered: list[dict[str, Any]] = []
    if app.chokepoint is not None:
        probe = ToolContext(task_id="00000000-0000-0000-0000-000000000000", agent_id=f"human:{user.user_id}", user=user,
                            actor=actor_facts_for_task(user, app.registry, classification),
                            task_classification=classification, db=app.db, data_dir=app.data_dir,
                            registry=app.registry, registry_dir=app.registry_dir, boundary=app.boundary)
        offered = [{"name": t["name"], "available": t["available"], "why_not": t["why_not"],
                    "side_effect": t["side_effect"], "description": t["description"],
                    "offered_in": (t.get("options") or {}).get("offered_in"), "schema": t["schema"]}
                   for t in app.chokepoint.available(probe)]
    tasks = {t["id"]: t for t in _visible_tasks(app, user)}
    live = [tid for tid, t in tasks.items() if t["status"] in ("planning", "running")]
    names, people = _names(app, list(tasks)), _people(app)
    running = []
    if live:
        for row in app.db.query(
            "SELECT DISTINCT ON (task_id, coalesce(agent_id, '')) task_id::text AS task_id, agent_id, step_type, payload, "
            "created_at FROM task_journal WHERE task_id = ANY(%(ids)s::uuid[]) AND step_type IN ('tool_call', 'tool_result', "
            "'model_call', 'agent') ORDER BY task_id, coalesce(agent_id, ''), step_seq DESC", {"ids": live},
        ):
            if row["step_type"] != "tool_call":
                continue
            payload = row["payload"] or {}
            tool = str(payload.get("tool") or "")
            running.append({"task_id": row["task_id"], "task": tasks[row["task_id"]].get("title"),
                            "who": _who(row.get("agent_id"), row["task_id"], names, people), "tool": tool,
                            "label": _SEARCH_LABELS.get(tool, tool), "detail": _args_brief(payload.get("arguments")),
                            "since": row["created_at"]})
    history = []
    ids = list(tasks)[:30]
    if ids:
        for row in app.db.query(
            "SELECT task_id::text AS task_id, agent_id, payload ->> 'tool' AS tool, payload ->> 'status' AS status, "
            "payload ->> 'summary' AS summary, (payload ->> 'duration_ms')::int AS duration_ms, created_at FROM task_journal "
            "WHERE task_id = ANY(%(ids)s::uuid[]) AND step_type = 'tool_result' ORDER BY id DESC LIMIT 40", {"ids": ids},
        ):
            history.append({**row, "task": tasks[row["task_id"]].get("title"),
                            "who": _who(row.get("agent_id"), row["task_id"], names, people)})
    active = sorted({r["tool"] for r in running})
    return {"available": offered, "running": running, "active": active, "history": history,
            "mcp_servers": [], "about": "Tools are in-process plugins declared in registry/tools.yaml, each call "
                                        "through the policy chokepoint. This deployment runs no MCP servers."}


async def tools_status(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    return JSONResponse(await blocking(gather_tools, app, user))


# -- environment and sandbox state ----------------------------------------------------------------------------


def _phase(task: Mapping[str, Any], agents: Sequence[Mapping[str, Any]], last: Optional[Mapping[str, Any]]) -> str:
    status = str(task["status"])
    if status == "running" and agents:
        helpers = [a for a in agents if a["agent_id"] != "lead"]
        busy = [a for a in helpers if a["status"] == "running"]
        if busy:
            return f"{len(busy)} of {len(helpers)} helper agent(s) working"
        if all(a["status"] in ("done", "failed") for a in helpers):
            return "the lead is writing the result from the team's findings"
        return "agents waiting on each other's findings"
    if status == "running" and last is not None:
        return {"tool_call": "using a tool", "tool_result": "reading a result", "model_call": "deciding the next step",
                "thought": "deciding the next step"}.get(str(last["step_type"]), "working")
    return {"submitted": "queued", "planning": "planning", "paused": "paused by a person",
            "revision_required": "queued for its revision", "awaiting_approval": "waiting for an approver",
            "completed": "done", "failed": "failed", "cancelled": "cancelled"}.get(status, status)


def gather_task_state(app: AppState, task: Mapping[str, Any]) -> dict[str, Any]:
    """The sandbox-state tree for one task: task, shared state, agents, artifacts,
    resources, and its event history -- all read from the tables the runtime writes."""
    task_id = str(task["id"])
    journal = Journal(app.db, task_id).entries(limit=4000)
    agents = app.db.query(
        "SELECT agent_id, name, role, goal, focus, depends_on, status, plan, current_step, findings, evidence, usage, "
        "started_at, finished_at FROM task_agents WHERE task_id = %(t)s::uuid ORDER BY (agent_id = 'lead'), agent_id",
        {"t": task_id},
    )
    shared = app.db.query(
        "SELECT id, kind, content, evidence, agent_id, author, addressed_to, created_at FROM task_shared_state "
        "WHERE task_id = %(t)s::uuid ORDER BY id", {"t": task_id},
    )
    memory = {str(r["key"]): r["value"] for r in app.db.query(
        "SELECT key, value FROM task_memory WHERE task_id = %(t)s::uuid ORDER BY key", {"t": task_id})}
    names, people = _names(app, [task_id]), _people(app)
    per_agent: dict[str, dict[str, Any]] = {}
    for entry in journal:
        aid = entry.get("agent_id") or ("lead" if entry["step_type"] in ("model_call", "thought", "tool_call", "tool_result") else None)
        if aid is None:
            continue
        slot = per_agent.setdefault(aid, {"steps": 0, "tool_calls": 0, "last": None, "tools": [], "thoughts": []})
        payload = entry.get("payload") or {}
        if entry["step_type"] == "model_call" and payload.get("purpose") == "act":
            slot["steps"] += 1
        elif entry["step_type"] == "tool_result":
            slot["tool_calls"] += 1
            slot["tools"].append({"tool": payload.get("tool"), "status": payload.get("status"),
                                  "summary": payload.get("summary")})
        elif entry["step_type"] == "thought":
            slot["thoughts"].append(str(payload.get("text") or "")[:300])
        slot["last"] = entry
    limits = BudgetLimits()
    plan = task.get("plan") or memory.get("plan") or {}
    rows = agents or ([{"agent_id": "lead", "name": "Lead agent", "role": "lead", "goal": task["goal"], "focus": [],
                        "depends_on": [], "status": task["status"], "plan": plan, "current_step": None,
                        "findings": (task.get("result") or {}).get("answer"), "evidence": [], "usage": task.get("usage") or {}}]
                      if journal and any(e["step_type"] == "claimed" for e in journal) else [])
    agent_view = []
    for agent in rows:
        aid = str(agent["agent_id"])
        slot = per_agent.get(aid, {"steps": 0, "tool_calls": 0, "tools": [], "thoughts": [], "last": None})
        limit = limits.max_steps if aid == "lead" else limits.agent_steps
        agent_view.append({
            "agent_id": aid, "name": agent["name"], "role": agent["role"], "status": agent["status"],
            "goal": agent["goal"], "focus": agent.get("focus") or [], "depends_on": agent.get("depends_on") or [],
            "waiting_for": [names.get((task_id, d), d) for d in agent.get("depends_on") or []
                            if next((a for a in rows if a["agent_id"] == d and a["status"] not in ("done", "failed")), None)],
            "current_step": agent.get("current_step"),
            "plan": ((agent.get("plan") or {}).get("steps") if isinstance(agent.get("plan"), dict) else None) or [],
            "progress": {"steps": slot["steps"], "limit": limit, "tool_calls": slot["tool_calls"]},
            "completed": [t["summary"] for t in slot["tools"] if t.get("status") == "ok"][-6:],
            "using": (slot["last"] or {}).get("payload", {}).get("tool") if (slot["last"] or {}).get("step_type") == "tool_call" else None,
            "working_memory": {"last_thought": slot["thoughts"][-1] if slot["thoughts"] else None,
                               "evidence": agent.get("evidence") or [], "findings": agent.get("findings")},
        })
    files: list[dict[str, Any]] = []
    root = app.data_dir.workspace(task_id)
    if root.is_dir():
        files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size} for p in sorted(root.rglob("*")) if p.is_file()]
    code = [{"step": (e.get("payload") or {}).get("step"), "who": _who(e.get("agent_id"), task_id, names, people),
             "source": str(((e.get("payload") or {}).get("arguments") or {}).get("source") or "")[:4000]}
            for e in journal if e["step_type"] == "tool_call" and (e.get("payload") or {}).get("tool") == "code.run"]
    outputs = app.db.query(
        "SELECT id::text AS id, title, template_id, filename, kind, status, version, created_at FROM artifacts "
        "WHERE task_id = %(t)s::uuid ORDER BY created_at", {"t": task_id},
    )
    claimed = next((e.get("payload") or {} for e in reversed(journal) if e["step_type"] == "claimed"), {})
    models = sorted({str((e.get("payload") or {}).get("model_id")) for e in journal
                     if e["step_type"] == "model_call" and (e.get("payload") or {}).get("model_id")})
    counts: dict[str, int] = {}
    for entry in journal:
        counts[entry["step_type"]] = counts.get(entry["step_type"], 0) + 1
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in ("plan", "decision", "fact", "assumption", "question", "note")}
    for row in shared:
        by_kind.setdefault(str(row["kind"]), []).append({
            "id": row["id"], "content": row["content"], "evidence": row["evidence"],
            "by": _who(_author(row), task_id, names, people),
            "to": names.get((task_id, str(row["addressed_to"]))) if row.get("addressed_to") else None,
            "at": row["created_at"],
        })
    recent = []
    for entry in journal[-30:]:
        if entry["step_type"] in _ACTIVITY_KINDS:
            text, level = activity_line(entry, _who(entry.get("agent_id"), task_id, names, people))
            recent.append({"seq": entry["step_seq"], "at": entry["created_at"], "kind": entry["step_type"], "text": text,
                           "level": level})
    return {
        "task": {
            "id": task_id, "title": task.get("title"), "objective": task["goal"], "kind": task.get("kind"),
            "status": task["status"], "phase": _phase(task, agents, journal[-1] if journal else None),
            "classification": str(task["classification"]).upper(), "submitted_by": task.get("submitted_by_name"),
            "constraints": {"classification": str(task["classification"]).upper(),
                            "deliverable": plan.get("deliverable") if isinstance(plan, dict) else None,
                            "budgets": {"steps": limits.max_steps, "agent_steps": limits.agent_steps,
                                        "tokens": limits.max_tokens, "seconds": limits.max_seconds},
                            "pause_requested": task.get("pause_requested")},
            "usage": task.get("usage") or {}, "created_at": task.get("created_at"), "error": task.get("error"),
        },
        "shared_state": {"plan": (plan.get("steps") if isinstance(plan, dict) else None) or [],
                         "understanding": plan.get("understanding") if isinstance(plan, dict) else None,
                         **{f"{k}s" if not k.endswith("s") else k: v for k, v in by_kind.items() if k != "plan"},
                         "entries": len(shared)},
        "agents": agent_view,
        "artifacts": {"files": files, "datasets": [f for f in files if f["path"].lower().endswith((".csv", ".xlsx", ".json"))],
                      "generated_code": code, "outputs": outputs},
        "resources": {"tools": claimed.get("tools") or [], "withheld_tools": claimed.get("withheld_tools") or [],
                      "mcp_servers": [], "models": models},
        "working_memory": {k: v for k, v in memory.items() if k in ("plan", "memories", "revision_request")},
        "evidence": task_evidence(app.db, task_id)[:80],
        "history": {"counts": {"actions": counts.get("tool_call", 0) + counts.get("human", 0),
                               "tool_calls": counts.get("tool_call", 0),
                               "state_transitions": sum(counts.get(k, 0) for k in (
                                   "claimed", "planned", "replanned", "paused", "resumed", "finished", "failed",
                                   "cancelled", "revision", "revision_requested", "decision", "agent")),
                               "observations": counts.get("tool_result", 0), "model_calls": counts.get("model_call", 0),
                               "human_steps": sum(1 for e in journal if str(e.get("agent_id") or "").startswith("human:"))},
                    "recent": recent},
    }


async def task_state(request: Request) -> Response:
    app, user, task = await _task_for(request)
    if task is None:
        return error("no such task", 404)
    return JSONResponse(await blocking(gather_task_state, app, task))


def gather_environment(app: AppState, user: User) -> dict[str, Any]:
    """The overview the right-hand panel opens with: every task this person may see,
    grouped by state, the agents working now, recent shared-state entries, outputs, and
    the resources the workbench has."""
    tasks = _visible_tasks(app, user)
    ids = [t["id"] for t in tasks]
    names, people = _names(app, ids), _people(app)
    agents = app.db.query(
        "SELECT a.task_id::text AS task_id, a.agent_id, a.name, a.status, a.goal, a.current_step, a.depends_on "
        "FROM task_agents a JOIN tasks t ON t.id = a.task_id WHERE a.task_id = ANY(%(ids)s::uuid[]) "
        "AND t.status IN ('planning', 'running', 'paused') ORDER BY a.task_id, a.agent_id", {"ids": ids or ["00000000-0000-0000-0000-000000000000"]},
    )
    shared = app.db.query(
        "SELECT task_id::text AS task_id, kind, content, evidence, agent_id, author, created_at FROM task_shared_state "
        "WHERE task_id = ANY(%(ids)s::uuid[]) AND kind <> 'plan' ORDER BY id DESC LIMIT 20",
        {"ids": ids or ["00000000-0000-0000-0000-000000000000"]},
    )
    outputs = app.db.query(
        "SELECT id::text AS id, task_id::text AS task_id, title, status, kind, version, created_at FROM artifacts "
        "WHERE task_id = ANY(%(ids)s::uuid[]) ORDER BY created_at DESC LIMIT 12",
        {"ids": ids or ["00000000-0000-0000-0000-000000000000"]},
    )
    groups: dict[str, int] = {}
    for task in tasks:
        groups[str(task["status"])] = groups.get(str(task["status"]), 0) + 1
    models = []
    try:
        models = [{"id": m["id"], "installed": m.get("installed"), "enabled": m.get("enabled"),
                   "capabilities": m.get("capabilities")} for m in app.gateway.status().get("models", [])]
    except Exception:
        models = []
    return {
        "tasks": {"by_status": groups, "active": [
            {"id": t["id"], "title": t.get("title"), "kind": t.get("kind"), "status": t["status"]}
            for t in tasks if t["status"] not in TERMINAL][:20]},
        "agents": [{**a, "who": names.get((a["task_id"], a["agent_id"]), a["name"])} for a in agents],
        "shared_state": [{"task_id": s["task_id"], "kind": s["kind"], "content": s["content"], "evidence": s["evidence"],
                          "by": _who(_author(s), s["task_id"], names, people), "at": s["created_at"]}
                         for s in shared],
        "artifacts": outputs,
        "resources": {"models": models, "mcp_servers": [],
                      "tools": [t.name for t in app.registry.tools],
                      "sandbox": "container" if app.sandbox is not None else "process"},
    }


async def environment(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    return JSONResponse(await blocking(gather_environment, app, user))


__all__ = [
    "revise_task",
    "pause_task",
    "resume_task",
    "add_note",
    "run_tool",
    "drafts_index",
    "create_draft",
    "update_draft",
    "delete_draft",
    "commit_draft",
    "activity",
    "activity_line",
    "tools_status",
    "task_state",
    "environment",
    "gather_activity",
    "gather_tools",
    "gather_task_state",
    "gather_environment",
]
