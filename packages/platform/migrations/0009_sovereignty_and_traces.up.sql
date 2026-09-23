-- 0009_sovereignty_and_traces -- the two remaining record types.
--
-- egress_events is the sovereignty telemetry (packages/sovereignty/AGENTS.md): every
-- outbound attempt, block and DNS denial the monitors observe, attributed to a task
-- and an agent where one was active. It is written by the telemetry side only and
-- never read by the enforcement side -- the pair is evidence precisely because neither
-- depends on the other. The governance-relevant subset (egress.attempt/.blocked,
-- dns.denied) is ALSO written to audit_log by the same code path, with a different,
-- smaller payload: this table is the queryable operational detail, audit_log is the
-- legal record that it happened.
CREATE TABLE egress_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    process     TEXT NOT NULL,     -- api | worker | sandbox | probe
    kind        TEXT NOT NULL CHECK (kind IN ('attempt', 'blocked', 'dns_denied', 'observed')),
    detector    TEXT NOT NULL,     -- guard | monitor | sandbox
    destination TEXT NOT NULL,
    port        INTEGER,
    task_id     UUID,
    agent_id    TEXT,
    detail      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX egress_events_task_idx ON egress_events (task_id, occurred_at);
CREATE INDEX egress_events_time_idx ON egress_events (occurred_at);

-- The execution trace (packages/platform/AGENTS.md's three record types): spans for
-- tasks, plan/act steps, tool calls, model calls and ingestion. Sampleable and
-- retained by policy -- NOT the audit chain, and never hash-chained. Metrics
-- (latency, error rate, tokens) are aggregated from here on read.
CREATE TABLE trace_spans (
    span_id     UUID PRIMARY KEY,
    parent_id   UUID,
    task_id     UUID,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- task | step | tool | model | ingest | http
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    duration_ms INTEGER,
    status      TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    attributes  JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX trace_spans_task_idx ON trace_spans (task_id, started_at);
CREATE INDEX trace_spans_kind_idx ON trace_spans (kind, started_at);

GRANT SELECT, INSERT ON egress_events TO citadel_app;
GRANT SELECT, INSERT ON trace_spans TO citadel_app;
