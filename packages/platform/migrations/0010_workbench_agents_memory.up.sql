-- 0010_workbench_agents_memory -- what the workbench (the shared place where people and
-- agents do the work), multi-agent tasks and the memory manager need.
--
-- Nothing here loosens an earlier rule: the journal stays append-only, memories carry
-- a classification and an ACL exactly as documents do and are filtered by them in the
-- same statement that searches them, and every human step is journalled like an
-- agent's.

-- Tasks gain a pause (distinct from cancel: a paused task keeps its journal and its
-- agents and resumes where it stopped) and a kind: 'task' produces work, 'ask' answers
-- a question about the corpus or about the workbench's own state (/ask in the CLI).
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
    'submitted', 'planning', 'running', 'paused', 'revision_required',
    'awaiting_approval', 'completed', 'failed', 'cancelled'
));
ALTER TABLE tasks
    ADD COLUMN kind            TEXT NOT NULL DEFAULT 'task' CHECK (kind IN ('task', 'ask')),
    ADD COLUMN pause_requested BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN draft_id        UUID;

-- Which agent wrote a journal entry: NULL for the task itself (submission, claims,
-- endings), 'lead' for the coordinating agent, 'agent_1'... for the agents it spawned,
-- 'human:<external identity>' for a step a person took in the workbench.
ALTER TABLE task_journal ADD COLUMN agent_id TEXT;
CREATE INDEX task_journal_agent_idx ON task_journal (task_id, agent_id, step_seq);

-- The agents working on a task. There is no UNIQUE(task_id): one task, many agents
-- (root AGENTS.md, "UNIQUE(task_id) on agents"). depends_on names the agents whose
-- findings an agent waits for before it starts.
CREATE TABLE task_agents (
    task_id      UUID NOT NULL REFERENCES tasks(id),
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL,          -- open: lead, researcher, analyst, writer, ...
    goal         TEXT NOT NULL,
    focus        TEXT[] NOT NULL DEFAULT '{}',   -- tools the planner suggested for it
    depends_on   TEXT[] NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'waiting', 'paused', 'done', 'failed', 'cancelled')),
    plan         JSONB,
    current_step TEXT,
    findings     TEXT,
    evidence     TEXT[] NOT NULL DEFAULT '{}',
    usage        JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    PRIMARY KEY (task_id, agent_id)
);

CREATE TRIGGER task_agents_set_updated_at
    BEFORE UPDATE ON task_agents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The shared state of a task: the plan everyone works to, decisions taken, facts
-- discovered (with the evidence ids that ground them), assumptions made, and notes or
-- questions -- written by agents and by people alike. Never edited: a wrong entry is
-- retracted by a later one, so the history of what was believed stays readable.
CREATE TABLE task_shared_state (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id    UUID NOT NULL REFERENCES tasks(id),
    kind       TEXT NOT NULL CHECK (kind IN ('plan', 'decision', 'fact', 'assumption', 'note', 'question')),
    content    TEXT NOT NULL,
    evidence   TEXT[] NOT NULL DEFAULT '{}',
    agent_id   TEXT,                   -- who wrote it: an agent id, or NULL for a person
    author     TEXT NOT NULL,          -- agent id or 'human:<external identity>'
    addressed_to TEXT,                 -- an agent id when a person steers one agent
    retracts   BIGINT REFERENCES task_shared_state(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX task_shared_state_task_idx ON task_shared_state (task_id, id);

-- /task <name> opens a draft: a task file the person writes the detailed goal into
-- and commits when ready. Committing creates the task; the draft keeps the link.
CREATE TABLE task_drafts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner             TEXT NOT NULL,     -- external identity, from the verified session
    name              TEXT NOT NULL,
    body              TEXT NOT NULL DEFAULT '',
    classification    TEXT,
    committed_task_id UUID REFERENCES tasks(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX task_drafts_owner_idx ON task_drafts (owner, updated_at DESC);

CREATE TRIGGER task_drafts_set_updated_at
    BEFORE UPDATE ON task_drafts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Documents live in folders (COMPANY_POLICIES/..., MANUFACTURING_DEPT/sector1, ...)
-- purely for people to find them: a folder grants nothing, the ACL does. And a
-- document can be re-issued: every version stays readable and citable, the newest is
-- what search returns.
ALTER TABLE documents ADD COLUMN folder TEXT NOT NULL DEFAULT '';
CREATE INDEX documents_folder_idx ON documents (folder);

CREATE TABLE document_versions (
    document_id UUID NOT NULL REFERENCES documents(id),
    version     INTEGER NOT NULL CHECK (version >= 1),
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    change_note TEXT,
    effective   DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, version)
);

INSERT INTO document_versions (document_id, version, filename, sha256, storage_ref, uploaded_by, created_at)
SELECT id, version, filename, sha256, storage_ref, uploaded_by, created_at FROM documents;

-- The memory manager (packages/memory, docs/adr/0009): Monarch's design -- extract
-- candidate memories, decide how each mutates what is already remembered, execute that
-- deterministically, retrieve by meaning and recency -- in Citadel's store, with the
-- one thing Monarch's store lacked: every memory carries a classification and an ACL,
-- and retrieval filters by them in the same statement as the vector search.
CREATE TABLE memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tier            TEXT NOT NULL CHECK (tier IN ('episodic', 'semantic')),
    memory_type     TEXT NOT NULL,     -- open vocabulary (equipment_fact, outcome, lesson ...), checked by shape only
    subject         TEXT,
    content         TEXT NOT NULL,
    classification  TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential')),
    acl             TEXT[] NOT NULL CHECK (cardinality(acl) > 0),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'archived', 'invalid')),
    certainty       TEXT,
    temporal_scope  TEXT,
    source          JSONB NOT NULL DEFAULT '{}',   -- task, agent, evidence ids, author
    embedding       VECTOR(768),
    embedding_model TEXT,
    superseded_by   UUID REFERENCES memories(id),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INTEGER NOT NULL DEFAULT 0,
    tsv             TSVECTOR
);

CREATE INDEX memories_embedding_hnsw ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX memories_tsv_idx ON memories USING gin (tsv);
CREATE INDEX memories_status_idx ON memories (status, tier);
CREATE INDEX memories_acl_idx ON memories USING gin (acl);

CREATE TRIGGER memories_set_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Every decision the memory manager takes, including "ignore": what was proposed, what
-- it was compared with, what was done and why. The record a person reads to trust (or
-- correct) what the workbench remembers.
CREATE TABLE memory_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id   UUID REFERENCES memories(id),
    operation   TEXT NOT NULL CHECK (operation IN (
                    'create', 'update', 'merge', 'contradict', 'ignore', 'archive', 'restore', 'edit')),
    candidate   TEXT,
    target_id   UUID,
    replacement TEXT,
    reason      TEXT,
    confidence  REAL,
    task_id     UUID,
    actor       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX memory_events_time_idx ON memory_events (created_at DESC);
CREATE INDEX memory_events_task_idx ON memory_events (task_id);

-- Which processes are alive: each service writes its row every few seconds. The
-- container-health panel reads last_seen; nothing else depends on it.
CREATE TABLE service_heartbeats (
    service    TEXT NOT NULL,
    instance   TEXT NOT NULL,
    pid        INTEGER,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail     JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (service, instance)
);

GRANT SELECT, INSERT, UPDATE ON task_agents TO citadel_app;
GRANT SELECT, INSERT ON task_shared_state TO citadel_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON task_drafts TO citadel_app;  -- an uncommitted draft may be thrown away
GRANT SELECT, INSERT ON document_versions TO citadel_app;
GRANT SELECT, INSERT, UPDATE ON memories TO citadel_app;
GRANT SELECT, INSERT ON memory_events TO citadel_app;
GRANT SELECT, INSERT, UPDATE ON service_heartbeats TO citadel_app;
