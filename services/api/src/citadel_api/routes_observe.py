"""Observability, metrics and evaluation: the right-hand panel's list.

Model evaluation, agent behaviour, database monitoring, alerts, security, policy
auditing, container health and resource metrics -- each computed on read from the
records the system already keeps (the trace, the journal, the audit chain, the egress
record, the verification ladder's results, the services' heartbeats, Postgres's own
statistics). Nothing here is a counter kept for the dashboard's sake, so nothing here
can drift from what actually happened.
"""

from __future__ import annotations

import shutil
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from citadel_platform import heartbeat
from citadel_sovereignty import enforcement_status
from citadel_sovereignty import status as sovereignty_status

from citadel_api.common import blocking, state
from citadel_api.deps import AppState, require_user

SECTIONS = ("models", "agents", "database", "alerts", "security", "policy", "containers", "resources")


def model_evaluation(app: AppState, hours: int) -> dict[str, Any]:
    window = {"h": hours}
    per_model = app.db.query(
        "SELECT attributes ->> 'model_id' AS model_id, attributes ->> 'purpose' AS purpose, count(*) AS calls, "
        "round(avg(duration_ms)) AS avg_ms, "
        "round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric) AS p95_ms, "
        "sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
        "sum(CASE WHEN (attributes ->> 'fallback_used')::boolean THEN 1 ELSE 0 END) AS fallbacks, "
        "sum(CASE WHEN coalesce((attributes ->> 'attempts')::int, 1) > 1 THEN 1 ELSE 0 END) AS retried, "
        "sum(coalesce((attributes -> 'usage' ->> 'prompt')::int, 0)) AS prompt_tokens, "
        "sum(coalesce((attributes -> 'usage' ->> 'completion')::int, 0)) AS completion_tokens "
        "FROM trace_spans WHERE kind = 'model' AND started_at > now() - make_interval(hours => %(h)s) "
        "AND attributes ? 'model_id' GROUP BY 1, 2 ORDER BY 3 DESC",
        window,
    )
    verification = app.db.query(
        "SELECT t ->> 'name' AS tier, t ->> 'status' AS status, count(*) AS n FROM artifacts a, "
        "jsonb_array_elements(coalesce(a.verification -> 'tiers', '[]'::jsonb)) t "
        "WHERE a.created_at > now() - make_interval(hours => %(h)s) GROUP BY 1, 2 ORDER BY 1, 2",
        window,
    )
    grounding = app.db.query_one(
        "SELECT coalesce(sum((t -> 'details' ->> 'traced')::int), 0) AS traced, "
        "coalesce(sum(jsonb_array_length(coalesce(t -> 'details' -> 'ungrounded', '[]'::jsonb))), 0) AS ungrounded "
        "FROM artifacts a, jsonb_array_elements(coalesce(a.verification -> 'tiers', '[]'::jsonb)) t "
        "WHERE t ->> 'name' = 'grounding' AND a.created_at > now() - make_interval(hours => %(h)s)",
        window,
    ) or {"traced": 0, "ungrounded": 0}
    answers = app.db.query_one(
        "SELECT count(*) AS finished, "
        "sum(CASE WHEN jsonb_array_length(coalesce(result -> 'citations', '[]'::jsonb)) > 0 THEN 1 ELSE 0 END) AS cited "
        "FROM tasks WHERE status IN ('completed', 'awaiting_approval') AND finished_at IS NOT NULL "
        "AND created_at > now() - make_interval(hours => %(h)s)",
        window,
    ) or {"finished": 0, "cited": 0}
    traced = int(grounding.get("traced") or 0)
    ungrounded = int(grounding.get("ungrounded") or 0)
    return {
        "per_model": per_model,
        "verification_by_tier": verification,
        "grounding_rate": round(traced / (traced + ungrounded), 3) if traced + ungrounded else None,
        "answers_with_citations": {"finished": int(answers.get("finished") or 0), "cited": int(answers.get("cited") or 0)},
        "about": ("Measured on live work: latency, errors, fallbacks and retries per model and purpose from the trace; "
                  "output quality from the verification ladder (by tier) and the grounding of quantitative claims."),
    }


def agent_behaviour(app: AppState, hours: int) -> dict[str, Any]:
    window = {"h": hours}
    tasks = app.db.query(
        "SELECT kind, status, count(*) AS n, round(avg(coalesce((usage ->> 'steps')::int, 0)), 1) AS avg_steps, "
        "round(avg(coalesce((usage ->> 'tool_calls')::int, 0)), 1) AS avg_tool_calls, "
        "round(avg(extract(epoch FROM finished_at - started_at))::numeric, 1) AS avg_seconds "
        "FROM tasks WHERE created_at > now() - make_interval(hours => %(h)s) GROUP BY kind, status ORDER BY kind, status",
        window,
    )
    journal = app.db.query(
        "SELECT step_type, count(*) AS n FROM task_journal WHERE created_at > now() - make_interval(hours => %(h)s) "
        "AND step_type IN ('replanned', 'paused', 'resumed', 'steered', 'revision', 'human', 'recalled', 'memory', "
        "'agents', 'failed') GROUP BY step_type ORDER BY step_type",
        window,
    )
    tools = app.db.query(
        "SELECT payload ->> 'tool' AS tool, payload ->> 'status' AS status, count(*) AS n FROM task_journal "
        "WHERE step_type = 'tool_result' AND created_at > now() - make_interval(hours => %(h)s) GROUP BY 1, 2 ORDER BY 3 DESC",
        window,
    )
    teams = app.db.query_one(
        "SELECT count(DISTINCT task_id) AS team_tasks, count(*) FILTER (WHERE agent_id <> 'lead') AS helpers, "
        "count(*) FILTER (WHERE status = 'failed') AS failed_agents FROM task_agents "
        "WHERE created_at > now() - make_interval(hours => %(h)s)",
        window,
    ) or {}
    humans = app.db.scalar(
        "SELECT count(*) FROM task_journal WHERE agent_id LIKE 'human:%%' AND created_at > now() - make_interval(hours => %(h)s)",
        window,
    )
    failures = app.db.query(
        "SELECT CASE WHEN error LIKE '%%budget exceeded%%' THEN 'budget' WHEN error LIKE '%%model%%' THEN 'model runtime' "
        "WHEN error LIKE 'cancelled%%' THEN 'cancelled' ELSE 'other' END AS cause, count(*) AS n FROM tasks "
        "WHERE status IN ('failed', 'cancelled') AND created_at > now() - make_interval(hours => %(h)s) GROUP BY 1",
        window,
    )
    return {"tasks": tasks, "events": journal, "tools": tools, "teams": teams, "human_steps": int(humans or 0),
            "failures": failures}


def database(app: AppState) -> dict[str, Any]:
    size = app.db.query_one(
        "SELECT pg_database_size(current_database()) AS bytes, "
        "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS connections, "
        "(SELECT coalesce(max(extract(epoch FROM now() - query_start)), 0) FROM pg_stat_activity "
        " WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()) AS longest_query_s, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector, "
        "current_setting('server_version') AS postgres"
    ) or {}
    tables = app.db.query(
        "SELECT relname AS table, n_live_tup AS rows, pg_total_relation_size(relid) AS bytes, "
        "coalesce(seq_scan, 0) AS seq_scans, coalesce(idx_scan, 0) AS index_scans FROM pg_stat_user_tables "
        "ORDER BY pg_total_relation_size(relid) DESC LIMIT 14"
    )
    queues = {
        "ingestion": app.db.query("SELECT status, count(*) AS n FROM documents GROUP BY status ORDER BY status"),
        "tasks": app.db.query("SELECT status, count(*) AS n FROM tasks GROUP BY status ORDER BY status"),
    }
    return {"database": size, "tables": tables, "queues": queues}


def alerts(app: AppState) -> list[dict[str, Any]]:
    """What a person should look at now, most serious first. Derived, never stored."""
    found: list[dict[str, Any]] = []

    def add(level: str, title: str, detail: str, count: int = 1) -> None:
        if count:
            found.append({"level": level, "title": title, "detail": detail, "count": count})

    observed = int(app.db.scalar("SELECT count(*) FROM egress_events WHERE kind = 'observed' "
                                 "AND occurred_at > now() - interval '1 day'") or 0)
    add("alert", "External connection observed", "a process opened a connection outside the allowed networks "
        "in the last day -- see Sovereignty", observed)
    failed = int(app.db.scalar("SELECT count(*) FROM tasks WHERE status = 'failed' AND finished_at > now() - interval '1 day'") or 0)
    add("warn", "Tasks failed", "in the last day; the task view says why", failed)
    budget = int(app.db.scalar("SELECT count(*) FROM tasks WHERE error LIKE '%%budget exceeded%%' "
                               "AND finished_at > now() - interval '1 day'") or 0)
    add("warn", "Budgets exceeded", "tasks stopped at a step, token or time budget in the last day", budget)
    questions = int(app.db.scalar(
        "SELECT count(*) FROM task_shared_state s JOIN tasks t ON t.id = s.task_id WHERE s.kind = 'question' "
        "AND t.status NOT IN ('completed', 'failed', 'cancelled')") or 0)
    add("warn", "Open questions for a person", "agents raised questions (policy conflicts among them) on live tasks",
        questions)
    try:
        missing = [m["id"] for m in app.gateway.status().get("models", []) if m.get("enabled") and not m.get("installed")]
    except Exception:
        missing = []
    add("warn", "Approved models missing", ", ".join(missing) + " -- run `citadel models`", len(missing))
    stale = [s for s in heartbeat.services(app.db) if s.get("stale")]
    add("warn", "Services not reporting", ", ".join(f"{s['service']} ({s['seconds_since']} s)" for s in stale), len(stale))
    bad_docs = int(app.db.scalar("SELECT count(*) FROM documents WHERE status = 'failed'") or 0)
    add("warn", "Documents failed ingestion", "see Documents for the reason", bad_docs)
    flagged = int(app.db.scalar(
        "SELECT count(*) FROM artifacts WHERE status = 'VERIFIED' AND jsonb_array_length(coalesce(verification -> "
        "'flagged_claims', '[]'::jsonb)) > 0") or 0)
    add("info", "Deliverables with flagged claims", "awaiting a decision; the flags travel to the approver", flagged)
    denials = int(app.db.scalar("SELECT count(*) FROM audit_log WHERE event_name = 'policy.denial' "
                                "AND occurred_at > now() - interval '1 day'") or 0)
    add("info", "Policy denials", "tool calls the rules refused in the last day -- see Policy auditing", denials)
    order = {"alert": 0, "warn": 1, "info": 2}
    return sorted(found, key=lambda a: order.get(a["level"], 3))


def security(app: AppState) -> dict[str, Any]:
    events = app.db.query(
        "SELECT event_name, count(*) AS n FROM audit_log WHERE occurred_at > now() - interval '1 day' AND event_name IN "
        "('policy.decision', 'policy.denial', 'identity.verified', 'identity.discarded', 'receipt.issued', "
        "'receipt.rejected', 'retrieval.denied_doc', 'egress.attempt', 'egress.blocked', 'dns.denied', "
        "'sovereignty.probe', 'document.rejected') GROUP BY event_name ORDER BY event_name"
    )
    return {"last_day": events, "sovereignty": sovereignty_status(app.db, window_minutes=60 * 24),
            "enforcement": enforcement_status()}


def policy_audit(app: AppState) -> dict[str, Any]:
    by_rule = app.db.query(
        "SELECT event_name, payload ->> 'rule_id' AS rule_id, payload ->> 'tool' AS tool, count(*) AS n FROM audit_log "
        "WHERE event_name IN ('policy.decision', 'policy.denial') AND occurred_at > now() - interval '7 days' "
        "GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 40"
    )
    recent = app.db.query(
        "SELECT seq, event_name, occurred_at, actor_id, payload ->> 'tool' AS tool, payload ->> 'rule_id' AS rule_id, "
        "payload ->> 'reason' AS reason, payload ->> 'effect' AS effect, payload ->> 'agent_id' AS agent_id, "
        "payload ->> 'task_id' AS task_id FROM audit_log WHERE event_name IN ('policy.decision', 'policy.denial') "
        "ORDER BY seq DESC LIMIT 40"
    )
    total = int(app.db.scalar("SELECT count(*) FROM audit_log") or 0)
    return {"by_rule": by_rule, "recent": recent, "audit_rows": total,
            "rules": [{"id": r.id, "effect": r.effect, "reason": r.reason} for r in app.registry.policy]}


def containers(app: AppState) -> dict[str, Any]:
    services = heartbeat.services(app.db)
    live = heartbeat.process_stats()
    sandbox: dict[str, Any]
    if app.sandbox is not None:
        try:
            sandbox = {"ok": True, **app.sandbox.health()}
        except Exception as exc:
            sandbox = {"ok": False, "error": str(exc)[:200]}
    else:
        sandbox = {"ok": True, "kind": "process", "note": "no sandbox container on this deployment"}
    try:
        view = app.gateway.runtime_view(force=False)
        runtime = {"reachable": view.reachable, "endpoint": getattr(app.gateway.provider, "endpoint", None),
                   "loaded": list(getattr(view, "loaded", []) or [])}
    except Exception as exc:
        runtime = {"reachable": False, "error": str(exc)[:200]}
    return {"services": services, "api_process": live, "sandbox": sandbox, "inference_runtime": runtime,
            "postgres": {"ok": True}}


def resources(app: AppState) -> dict[str, Any]:
    host = heartbeat.host_stats()
    if app.data_dir.root.exists():
        disk = shutil.disk_usage(str(app.data_dir.root))
        host["data_disk"] = {"total_gb": round(disk.total / 1e9, 1), "free_gb": round(disk.free / 1e9, 1)}
    return {"host": host, "gpu_admission": app.gateway.gpu.snapshot(), "api_process": heartbeat.process_stats(),
            "note": "Host figures are the API container's view of the machine; each service reports its own "
                    "footprint under Container health."}


async def observability(request: Request) -> Response:
    app = state(request)
    require_user(request, app)
    wanted = [s for s in (request.query_params.get("sections") or ",".join(SECTIONS)).split(",") if s in SECTIONS]
    hours = max(1, min(int(request.query_params.get("hours") or 24), 24 * 30))

    panels: dict[str, Callable[[], Any]] = {
        "models": lambda: model_evaluation(app, hours),
        "agents": lambda: agent_behaviour(app, hours),
        "database": lambda: database(app),
        "alerts": lambda: alerts(app),
        "security": lambda: security(app),
        "policy": lambda: policy_audit(app),
        "containers": lambda: containers(app),
        "resources": lambda: resources(app),
    }

    def gather() -> dict[str, Any]:
        out: dict[str, Any] = {"hours": hours}
        for section in wanted:
            try:
                out[section] = panels[section]()
            except Exception as exc:  # one broken panel must not blank the others
                out[section] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return out

    return JSONResponse(await blocking(gather))


__all__ = ["observability", "SECTIONS"]
