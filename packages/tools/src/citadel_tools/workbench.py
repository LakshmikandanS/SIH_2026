"""workbench.inspect and state.note: the workbench as something agents (and people)
can look at and write to.

workbench.inspect answers /ask-style questions about work in progress -- which tasks,
which agents, what each is doing or found, what was decided -- from the same tables the
UI reads. It shows only tasks the asking person submitted, and only at or below the
asking task's classification: an INTERNAL question never reads a CONFIDENTIAL task's
findings.

state.note writes to a task's shared state (migration 0010): a fact with the evidence
ids it rests on, a decision, an assumption, or a question for a person. Evidence ids are
checked against what the task actually holds, so a note cannot cite something nobody
was shown. Agents and people use the same tool, through the same chokepoint.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from citadel_contracts.classification import Classification
from citadel_contracts.domain import Resource

from citadel_tools.context import Invocation, ToolContext, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin, digest_of, task_resource

_LATTICE = (Classification.PUBLIC, Classification.INTERNAL, Classification.CONFIDENTIAL)
_EVIDENCE = re.compile(r"^[EC]\d+$")
_UUIDISH = re.compile(r"^[0-9a-f]{4,}(-[0-9a-f]*)*$")
_NOTABLE = ("claimed", "planned", "replanned", "agents", "tool_result", "revision", "decision", "paused",
            "resumed", "finished", "failed", "cancelled", "human", "memory")


def _levels(ctx: ToolContext) -> list[str]:
    return [lvl.lower() for lvl in _LATTICE if not Classification.exceeds(lvl, ctx.actor.classification_max)]


def _inspect_resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    return task_resource(ctx, "workbench", digest_of(str(args.get("task") or "overview")))


def _match(tasks: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    ref = ref.strip().lower()
    if not ref:
        return None
    if _UUIDISH.match(ref):
        for task in tasks:
            if task["id"].startswith(ref):
                return task
    words = {w for w in re.findall(r"[a-z0-9]+", ref) if len(w) > 2}
    best, best_score = None, 0
    for task in tasks:
        title = f"{task.get('title') or ''} {task.get('goal') or ''}".lower()
        score = sum(1 for w in words if w in title)
        if score > best_score:
            best, best_score = task, score
    return best


def _line(entry: Mapping[str, Any]) -> str:
    payload = entry.get("payload") or {}
    kind = str(entry["step_type"])
    who = entry.get("agent_id") or "task"
    if kind == "tool_result":
        text = f"{payload.get('tool')}: {payload.get('status')} -- {payload.get('summary')}"
    elif kind in ("planned", "replanned"):
        steps = (payload.get("plan") or {}).get("steps") or []
        text = "plan: " + "; ".join(str(s) for s in steps[:6])
    elif kind == "agents":
        text = "spawned " + ", ".join(f"{a.get('name')} ({a.get('goal')})" for a in payload.get("agents") or [])
    elif kind in ("finished", "failed", "cancelled"):
        text = str(payload.get("answer") or payload.get("error") or payload.get("status") or "")[:300]
    elif kind == "human":
        text = f"{payload.get('by')}: {payload.get('action')} {payload.get('summary') or ''}"
    else:
        text = str(payload.get("text") or payload.get("comment") or payload.get("summary") or "")[:200]
    return f"[{str(entry['created_at'])[11:19]}] {who} {kind}: {text}"[:420]


def _inspect(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    tasks = ctx.db.query(
        "SELECT t.id::text AS id, t.title, t.goal, t.kind, t.status, t.classification, t.created_at, t.error, "
        "t.result ->> 'answer' AS answer FROM tasks t JOIN users u ON u.id = t.submitted_by "
        "WHERE u.external_identity = %(u)s AND t.classification = ANY(%(levels)s) AND t.id <> %(self)s::uuid "
        "ORDER BY t.created_at DESC LIMIT 15",
        {"u": ctx.user.user_id, "levels": _levels(ctx), "self": ctx.task_id},
    )
    ref = str(args.get("task") or "")
    target = _match(tasks, ref)
    if ref and target is None:
        raise ToolFailure(f"no task of yours at or below {ctx.actor.classification_max} matches {ref!r}")
    if target is None:
        overview = []
        for task in tasks[:10]:
            agents = ctx.db.query(
                "SELECT name, status, current_step FROM task_agents WHERE task_id = %(t)s::uuid ORDER BY agent_id",
                {"t": task["id"]},
            )
            overview.append({
                "task": task["id"][:8], "title": task["title"], "kind": task["kind"], "status": task["status"],
                "agents": [f"{a['name']}: {a['status']}" + (f" ({a['current_step']})" if a.get("current_step") else "")
                           for a in agents],
            })
        recent = ctx.db.query(
            "SELECT j.task_id::text AS task_id, j.step_type, j.payload, j.agent_id, j.created_at FROM task_journal j "
            "WHERE j.task_id = ANY(%(ids)s::uuid[]) AND j.step_type = ANY(%(kinds)s) ORDER BY j.id DESC LIMIT 12",
            {"ids": [t["id"] for t in tasks] or ["00000000-0000-0000-0000-000000000000"], "kinds": list(_NOTABLE)},
        )
        data = {"tasks": overview, "recent_activity": [_line(e) for e in reversed(recent)]}
        return ToolOutput(data=data, summary=f"overview of {len(overview)} task(s)")
    task_id = target["id"]
    agents = ctx.db.query(
        "SELECT agent_id, name, role, goal, status, current_step, depends_on, left(findings, 700) AS findings "
        "FROM task_agents WHERE task_id = %(t)s::uuid ORDER BY agent_id",
        {"t": task_id},
    )
    shared = ctx.db.query(
        "SELECT kind, content, evidence, author, created_at FROM task_shared_state WHERE task_id = %(t)s::uuid "
        "ORDER BY id DESC LIMIT 20",
        {"t": task_id},
    )
    journal = ctx.db.query(
        "SELECT step_type, payload, agent_id, created_at FROM task_journal WHERE task_id = %(t)s::uuid "
        "AND step_type = ANY(%(kinds)s) ORDER BY step_seq DESC LIMIT 14",
        {"t": task_id, "kinds": list(_NOTABLE)},
    )
    artifacts = ctx.db.query(
        "SELECT title, status, filename FROM artifacts WHERE task_id = %(t)s::uuid ORDER BY created_at", {"t": task_id}
    )
    data = {
        "task": {k: target[k] for k in ("id", "title", "kind", "status", "classification", "error") if target.get(k)},
        "answer": (target.get("answer") or "")[:900] or None,
        "agents": agents,
        "shared_state": [{"kind": s["kind"], "content": s["content"], "evidence": s["evidence"], "by": s["author"]}
                         for s in reversed(shared)],
        "recent_journal": [_line(e) for e in reversed(journal)],
        "artifacts": artifacts,
    }
    return ToolOutput(data=data, summary=f"state of task {task_id[:8]} ({target['status']}, {len(agents)} agent(s))")


def _note_resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    return task_resource(ctx, "shared-state", digest_of(f"{args.get('kind')}|{args.get('content')}"))


def _note(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    kind = str(args["kind"])
    content = " ".join(str(args["content"]).split())
    cited = [str(e).strip() for e in args.get("evidence") or [] if _EVIDENCE.match(str(e).strip())]
    if cited:
        held = {str(r["evidence_id"]) for r in ctx.db.query(
            "SELECT evidence_id FROM task_evidence WHERE task_id = %(t)s::uuid AND evidence_id = ANY(%(ids)s)",
            {"t": ctx.task_id, "ids": cited},
        )}
        unknown = [e for e in cited if e not in held]
        if unknown:
            raise ToolFailure(f"{', '.join(unknown)} is not evidence this task holds; cite only ids you were given")
    human = ctx.agent_id.startswith("human:")
    # An agent's context id carries the task (agent_1-1a2b3c4d); the shared state names
    # the agent as the task's own agent table does (agent_1). A person has no agent id.
    suffix = f"-{ctx.task_id[:8]}"
    agent = None if human else (ctx.agent_id[: -len(suffix)] if ctx.agent_id.endswith(suffix) else ctx.agent_id)
    row = ctx.db.query_one(
        "INSERT INTO task_shared_state (task_id, kind, content, evidence, agent_id, author) VALUES "
        "(%(t)s::uuid, %(k)s, %(c)s, %(e)s, %(a)s, %(by)s) RETURNING id",
        {"t": ctx.task_id, "k": kind, "c": content, "e": cited, "a": agent, "by": ctx.agent_id},
    )
    return ToolOutput(
        data={"recorded": kind, "id": row["id"] if row else None,
              "visible_to": "every agent on this task and the people working on it"},
        summary=f"{kind} added to the shared state: {content[:80]}",
    )


PLUGINS = {
    "workbench.inspect": ToolPlugin("workbench.inspect", _inspect_resource, _inspect),
    "state.note": ToolPlugin("state.note", _note_resource, _note),
}

__all__ = ["PLUGINS"]
