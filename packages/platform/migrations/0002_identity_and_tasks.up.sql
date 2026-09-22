-- 0002_identity_and_tasks -- schema for PLAN-M0 task 5's "users, tasks, journals",
-- and the shape task 7 (identity, roles, clearances, ACLs) will attach its verified-
-- session-token wiring to. This migration only creates the storage; task 7 does the
-- identity *logic* (packages/platform/AGENTS.md notwithstanding -- that file is
-- about the audit chain and registries, not this table).
--
-- classification columns are TEXT with a CHECK against the same three-level lattice
-- citadel_contracts.classification.Classification defines (public/internal/
-- confidential). That CHECK is membership only, never the lattice *comparison* --
-- root AGENTS.md invariant 4 ("a lattice with explicit comparison, never string
-- ordering") is about how two classifications are compared, which stays in Python.
-- A CHECK constraint does the same job here that Python's own AfterValidator does
-- for the registry loader: keep a typo from ever reaching the column.

CREATE TABLE users (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name      TEXT NOT NULL,
    department        TEXT NOT NULL,
    clearance         TEXT NOT NULL CHECK (clearance IN ('public', 'internal', 'confidential')),
    external_identity TEXT NOT NULL UNIQUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN users.external_identity IS
    'The subject claim from a verified session token. Never populated from a request '
    'body field -- root AGENTS.md invariant 3 / tests/structural/test_identity_not_from_body.py.';

CREATE TABLE tasks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal           TEXT NOT NULL,   -- the user's real goal text, verbatim -- never a hardcoded scenario (AGENTS.md "The hardcoded scenario")
    submitted_by   UUID NOT NULL REFERENCES users(id),
    classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential')),
    status         TEXT NOT NULL DEFAULT 'submitted'
                       CHECK (status IN ('submitted', 'planning', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Many journal rows per task, on purpose -- the whole point of a journal. No
-- UNIQUE(task_id)-shaped constraint anywhere in this file: AGENTS.md names
-- "UNIQUE(task_id) on agents" as the specific failure mode of encoding a scope cut
-- ("one agent per task") as a schema invariant. UNIQUE(task_id, step_seq) below is a
-- different thing -- it says each step *number* within a task is used once, not
-- that a task has only one row.
CREATE TABLE task_journal (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id    UUID NOT NULL REFERENCES tasks(id),
    step_seq   INTEGER NOT NULL,
    step_type  TEXT NOT NULL,
    payload    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, step_seq)
);

COMMENT ON TABLE task_journal IS
    'The step journal: what makes a task resumable after a worker restart -- AGENTS.md '
    '"Synchronous in-request execution" failure mode. Not the audit chain and not the '
    'execution trace (packages/platform/AGENTS.md''s three-record-types table) -- this '
    'is citadel_runtime''s own resumption state, not evidence.';

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tasks_set_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON users TO citadel_app;
GRANT SELECT, INSERT, UPDATE ON tasks TO citadel_app;
GRANT SELECT, INSERT ON task_journal TO citadel_app;  -- append-only in practice; no UPDATE needed
