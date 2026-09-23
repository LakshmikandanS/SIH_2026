"""What the sovereignty panel and the per-task report read.

Counts come from `egress_events` (what the telemetry recorded); the enforcement status
comes from what the container entrypoint wrote when it applied the ruleset. The report
never infers one from the other -- it shows both, side by side, because the pair is the
evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from citadel_platform.db import Database

ENFORCEMENT_FILE_VAR = "CITADEL_ENFORCEMENT_FILE"
DEFAULT_ENFORCEMENT_FILE = "/run/citadel/enforcement.json"


def enforcement_status(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """What the network-level enforcement reported when it was applied -- or, outside
    the container deployment, a plain statement that there is none."""
    env = env if env is not None else os.environ
    path = Path(env.get(ENFORCEMENT_FILE_VAR) or DEFAULT_ENFORCEMENT_FILE)
    if path.exists():
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(status, dict):
                return status
        except (OSError, ValueError) as exc:
            return {"mechanism": "unknown", "applied": False, "note": f"enforcement status unreadable: {exc}"}
    return {
        "mechanism": "none",
        "applied": False,
        "note": "not running in the container deployment: there is no network-level egress enforcement on this "
        "process, only the in-process fence. Run Citadel with citadel.cmd (Docker Compose) for the real boundary.",
    }


def _counts(db: Database, where: str, params: Mapping[str, Any]) -> dict[str, int]:
    row = db.query_one(
        "SELECT count(*) FILTER (WHERE kind = 'attempt') AS attempts, "
        "count(*) FILTER (WHERE kind = 'blocked') AS blocked, "
        "count(*) FILTER (WHERE kind = 'dns_denied') AS dns_denied, "
        "count(*) FILTER (WHERE kind = 'observed') AS observed, "
        "count(*) FILTER (WHERE detector = 'probe') AS probes "
        f"FROM egress_events WHERE {where}",
        dict(params),
    ) or {}
    return {k: int(row.get(k) or 0) for k in ("attempts", "blocked", "dns_denied", "observed", "probes")}


def status(db: Database, *, window_minutes: int = 60 * 24) -> dict[str, Any]:
    window = {"m": window_minutes}
    counts = _counts(db, "occurred_at > now() - make_interval(mins => %(m)s)", window)
    events = db.query(
        "SELECT occurred_at, process, kind, detector, destination, port, task_id::text AS task_id, agent_id, detail "
        "FROM egress_events ORDER BY occurred_at DESC LIMIT 25"
    )
    by_process = db.query(
        "SELECT process, kind, count(*) AS n FROM egress_events "
        "WHERE occurred_at > now() - make_interval(mins => %(m)s) GROUP BY process, kind ORDER BY process, kind",
        window,
    )
    model_calls = db.scalar(
        "SELECT count(*) FROM trace_spans WHERE kind = 'model' AND started_at > now() - make_interval(mins => %(m)s)",
        window,
    )
    return {
        "window_minutes": window_minutes,
        "counts": counts,
        "external_connections_observed": counts["observed"],
        "by_process": by_process,
        "recent": events,
        "model_calls": int(model_calls or 0),
    }


def task_report(db: Database, task_id: str) -> dict[str, Any]:
    """The exportable, attributable record for one task -- part of its provenance."""
    counts = _counts(db, "task_id = %(t)s::uuid", {"t": task_id})
    events = db.query(
        "SELECT occurred_at, process, kind, detector, destination, port, agent_id, detail FROM egress_events "
        "WHERE task_id = %(t)s::uuid ORDER BY occurred_at",
        {"t": task_id},
    )
    models = db.query(
        "SELECT attributes ->> 'model_id' AS model_id, attributes ->> 'purpose' AS purpose, count(*) AS calls "
        "FROM trace_spans WHERE task_id = %(t)s::uuid AND kind = 'model' GROUP BY 1, 2 ORDER BY 1, 2",
        {"t": task_id},
    )
    escaped = counts["observed"]
    verdict = (
        "No data left the deployment during this task."
        + (f" {counts['attempts'] + counts['dns_denied']} outbound attempt(s) were recorded and all were stopped."
           if counts["attempts"] or counts["blocked"] or counts["dns_denied"] else " No outbound attempt was made.")
        if not escaped
        else f"{escaped} external connection(s) were OBSERVED during this task -- see the events."
    )
    return {
        "task_id": task_id,
        "counts": counts,
        "events": events,
        "model_calls": models,
        "enforcement": enforcement_status(),
        "verdict": verdict,
    }


__all__ = ["enforcement_status", "status", "task_report", "ENFORCEMENT_FILE_VAR"]
