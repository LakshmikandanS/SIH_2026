-- 0003_artifacts_approvals_and_registry_state -- the remaining tables PLAN-M0 task 5
-- names: artifacts, approvals, and "the registries' runtime state".

CREATE TABLE artifacts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id        UUID NOT NULL REFERENCES tasks(id),
    kind           TEXT NOT NULL,   -- 'docx' | 'xlsx' | 'pptx' | 'code' | ... -- open: citadel_deliverables' template registry decides what's valid, not a CHECK here
    classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential')),
    storage_ref    TEXT NOT NULL,   -- where the bytes actually live; this table is metadata, not a blob store
    sha256         BYTEA NOT NULL CHECK (octet_length(sha256) = 32),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN artifacts.sha256 IS
    'Content hash for release integrity -- citadel_deliverables "release and hashing" '
    '(root AGENTS.md module map). Verified against storage_ref''s actual bytes at read '
    'time; this column is the recorded claim, not the proof.';

CREATE TABLE approvals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id  UUID NOT NULL REFERENCES artifacts(id),
    approver_id  UUID NOT NULL REFERENCES users(id),
    decision     TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason       TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);

COMMENT ON TABLE approvals IS
    'A decision here is not evidence by itself -- every approval and rejection is '
    'also written to audit_log by the application at the point of decision '
    '(packages/platform/AGENTS.md: "Denials are audit events"). This table is '
    'queryable current state; audit_log is the append-only record that it happened.';

-- Runtime state for a registry-defined model -- whether it is currently resident,
-- and the last health probe -- distinct from registry/models.*.yaml, which is the
-- *static* definition (root AGENTS.md invariant 1: no model identity or capacity
-- figure outside registry/). model_id is free TEXT, not a foreign key: the registry
-- lives in YAML, not in this database, on purpose -- citadel_platform's registry
-- loader, not a table, is the source of truth for which ids are valid.
CREATE TABLE model_runtime_state (
    model_id       TEXT PRIMARY KEY,
    resident       BOOLEAN NOT NULL DEFAULT false,
    last_health_ok BOOLEAN,
    last_probed_at TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER model_runtime_state_set_updated_at
    BEFORE UPDATE ON model_runtime_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON artifacts TO citadel_app;
GRANT SELECT, INSERT, UPDATE ON approvals TO citadel_app;
GRANT SELECT, INSERT, UPDATE ON model_runtime_state TO citadel_app;
