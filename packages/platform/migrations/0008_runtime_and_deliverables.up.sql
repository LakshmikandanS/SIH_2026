-- 0008_runtime_and_deliverables -- what the agent runtime and the deliverables
-- pipeline need on top of 0002/0003's tables (packages/runtime/AGENTS.md,
-- packages/deliverables/AGENTS.md).
--
-- tasks: the queue is this table. A worker claims the oldest 'submitted' row with
-- FOR UPDATE SKIP LOCKED, so any number of workers can pull concurrently without two
-- of them taking the same task, and nothing runs inside an HTTP request (root
-- AGENTS.md "Synchronous in-request execution").

ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
    'submitted', 'planning', 'running', 'revision_required',
    'awaiting_approval', 'completed', 'failed', 'cancelled'
));

ALTER TABLE tasks
    ADD COLUMN title              TEXT,
    ADD COLUMN requirements       JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN primary_capability TEXT,
    ADD COLUMN plan               JSONB,
    ADD COLUMN result             JSONB,
    ADD COLUMN error              TEXT,
    ADD COLUMN cancel_requested   BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN revision_count     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN worker_id          TEXT,
    ADD COLUMN usage              JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN started_at         TIMESTAMPTZ,
    ADD COLUMN finished_at        TIMESTAMPTZ;

CREATE INDEX tasks_queue_idx ON tasks (status, created_at);
CREATE INDEX tasks_submitted_by_idx ON tasks (submitted_by, created_at DESC);

-- Working memory: task-scoped, Citadel-side, Postgres (packages/memory/AGENTS.md).
-- Written deliberately by the runtime, never as a side effect of a model turn.
CREATE TABLE task_memory (
    task_id    UUID NOT NULL REFERENCES tasks(id),
    key        TEXT NOT NULL,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, key)
);

-- artifacts: status follows citadel_contracts.state_machines.ARTIFACT exactly
-- (TEMP -> CANDIDATE -> VERIFIED -> APPROVED -> RELEASED, no failure state). A draft
-- that fails verification stays TEMP and the agent may produce a new version; RELEASED
-- is terminal, which is what release immutability rests on.
ALTER TABLE artifacts
    ADD COLUMN title             TEXT,
    ADD COLUMN template_id       TEXT,
    ADD COLUMN filename          TEXT,
    ADD COLUMN mime_type         TEXT,
    ADD COLUMN status            TEXT NOT NULL DEFAULT 'TEMP'
                                     CHECK (status IN ('TEMP', 'CANDIDATE', 'VERIFIED', 'APPROVED', 'RELEASED')),
    ADD COLUMN version           INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN requires_approval BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN verification      JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN provenance        JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN created_by        TEXT,
    ADD COLUMN released_at       TIMESTAMPTZ,
    ADD COLUMN updated_at        TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX artifacts_task_idx ON artifacts (task_id, created_at);
CREATE INDEX artifacts_status_idx ON artifacts (status);

CREATE TRIGGER artifacts_set_updated_at
    BEFORE UPDATE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A released artifact is frozen: its bytes' hash and its provenance can no longer
-- change, whoever asks. Same belt-and-braces posture as audit_log's triggers in 0001.
CREATE OR REPLACE FUNCTION artifacts_freeze_released() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'RELEASED' THEN
        RAISE EXCEPTION 'artifact % is RELEASED and immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER artifacts_freeze_released_trigger
    BEFORE UPDATE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION artifacts_freeze_released();

CREATE INDEX approvals_artifact_idx ON approvals (artifact_id);

GRANT SELECT, INSERT, UPDATE ON task_memory TO citadel_app;
