"""The execution trace and the metrics derived from it (PLAN-M0 task 9).

Spans go to `trace_spans` (migration 0009). They are the operational record -- what
ran, for how long, with which model, how many tokens -- and deliberately never the
audit chain: packages/platform/AGENTS.md's three record types answer different
questions to different readers, so a call site that wants both writes an audit event
with the *governance* facts and a span with the *operational* ones, never the same
payload twice.

Tracing is best effort by design: a span that fails to write is reported on stderr and
dropped, and never fails the work it was measuring. The audit chain is the opposite --
an audit write that fails fails the operation.
"""

from __future__ import annotations

import contextvars
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from citadel_platform.db import Database, Json

_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "citadel_current_span", default=None
)
_current_task: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "citadel_current_task", default=None
)
_current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "citadel_current_agent", default=None
)


@dataclass
class Span:
    span_id: str
    name: str
    kind: str
    task_id: Optional[str]
    parent_id: Optional[str]
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class Tracer:
    def __init__(self, db: Optional[Database]) -> None:
        self._db = db

    @contextmanager
    def span(
        self,
        name: str,
        kind: str,
        *,
        task_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> Iterator[Span]:
        effective_task = task_id or _current_task.get()
        span = Span(
            span_id=str(uuid.uuid4()),
            name=name,
            kind=kind,
            task_id=effective_task,
            parent_id=_current_span.get(),
            attributes=dict(attributes or {}),
        )
        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        span_token = _current_span.set(span.span_id)
        task_token = _current_task.set(effective_task)
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.attributes.setdefault("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _current_span.reset(span_token)
            _current_task.reset(task_token)
            self._write(span, started, int((time.perf_counter() - clock) * 1000))

    def _write(self, span: Span, started: datetime, duration_ms: int) -> None:
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT INTO trace_spans (span_id, parent_id, task_id, name, kind, started_at, "
                "ended_at, duration_ms, status, attributes) VALUES (%(span_id)s::uuid, "
                "%(parent_id)s::uuid, %(task_id)s::uuid, %(name)s, %(kind)s, %(started)s, now(), "
                "%(duration)s, %(status)s, %(attributes)s)",
                {
                    "span_id": span.span_id,
                    "parent_id": span.parent_id,
                    "task_id": span.task_id,
                    "name": span.name,
                    "kind": span.kind,
                    "started": started,
                    "duration": duration_ms,
                    "status": span.status,
                    "attributes": Json(span.attributes),
                },
            )
        except Exception as exc:  # tracing never breaks the work it measures
            print(f"[trace] dropped span {span.name}: {exc}", file=sys.stderr)


def current_task_id() -> Optional[str]:
    return _current_task.get()


def current_agent_id() -> Optional[str]:
    return _current_agent.get()


@contextmanager
def task_context(task_id: Optional[str], agent_id: Optional[str] = None) -> Iterator[None]:
    """Attribute everything in this block -- spans, egress telemetry -- to a task and,
    where there is one, the agent acting for it."""
    token = _current_task.set(task_id)
    agent_token = _current_agent.set(agent_id)
    try:
        yield
    finally:
        _current_agent.reset(agent_token)
        _current_task.reset(token)


def metrics_summary(db: Database, *, window_minutes: int = 60) -> list[dict[str, Any]]:
    """Latency and error rate per span kind over a recent window -- the metrics the
    UI shows, aggregated from the trace on read rather than kept as separate counters."""
    return db.query(
        "SELECT kind, count(*) AS calls, "
        "round(avg(duration_ms)) AS avg_ms, "
        "round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::numeric) AS p50_ms, "
        "round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric) AS p95_ms, "
        "round(100.0 * sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) / count(*), 1) AS error_pct "
        "FROM trace_spans WHERE started_at > now() - make_interval(mins => %(window)s) "
        "GROUP BY kind ORDER BY kind",
        {"window": window_minutes},
    )


__all__ = ["Span", "Tracer", "current_task_id", "current_agent_id", "task_context", "metrics_summary"]
