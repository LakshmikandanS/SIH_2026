-- 0007_knowledge -- documents, their pages and layout blocks, and the chunk index
-- retrieval searches (packages/knowledge/AGENTS.md).
--
-- Classification and ACL are mandatory at the door: both columns are NOT NULL with no
-- default, and the ACL must name at least one department. A document the upload path
-- could not classify never gets a row at all (packages/knowledge/AGENTS.md: "rejected,
-- loudly. Never defaulted"). Classification values are lowercase here, the same
-- storage convention as 0002/0003; the lattice comparison itself stays in Python
-- (root AGENTS.md invariant 4) -- retrieval passes the *set* of permitted levels,
-- computed by citadel_contracts.classification.Classification, into the WHERE clause.
--
-- Documents are versioned and citations pin to a version: pages, blocks and chunks
-- all carry (document_id, version), and a re-ingested document keeps its older
-- version's rows so a citation made against v1 still resolves after v2 lands.

CREATE TABLE documents (
    id             UUID PRIMARY KEY,
    title          TEXT NOT NULL,
    filename       TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential')),
    acl            TEXT[] NOT NULL CHECK (cardinality(acl) > 0),
    uploaded_by    TEXT NOT NULL,   -- external_identity from a verified session token, never a body field
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    version        INTEGER NOT NULL DEFAULT 1,
    sha256         TEXT NOT NULL,
    storage_ref    TEXT NOT NULL,
    page_count     INTEGER,
    ingest_report  JSONB NOT NULL DEFAULT '{}',
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX documents_status_idx ON documents (status);

CREATE TRIGGER documents_set_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per rendered page. text_source records HOW the page's text was obtained --
-- a born-digital text layer, OCR, or neither -- because "degrade honestly" means a
-- reader can always see which pages were read by a machine that might be wrong.
CREATE TABLE document_pages (
    document_id    UUID NOT NULL REFERENCES documents(id),
    version        INTEGER NOT NULL,
    page           INTEGER NOT NULL CHECK (page >= 1),
    width_px       INTEGER,
    height_px      INTEGER,
    image_ref      TEXT,
    text_source    TEXT NOT NULL CHECK (text_source IN ('text_layer', 'ocr', 'none')),
    ocr_confidence REAL,
    vision         JSONB,   -- what the vision model reported at ingest, or why it did not run
    PRIMARY KEY (document_id, version, page)
);

-- The normalised document: pages -> blocks, each with a bounding box and a content
-- hash (packages/knowledge/AGENTS.md "Ingestion"). bbox is (x0, y0, x1, y1) normalised
-- to [0, 1] of the page image, matching citadel_contracts.domain.Evidence.bbox.
CREATE TABLE document_blocks (
    id             UUID PRIMARY KEY,
    document_id    UUID NOT NULL,
    version        INTEGER NOT NULL,
    page           INTEGER NOT NULL,
    block_index    INTEGER NOT NULL,
    kind           TEXT NOT NULL,          -- open: text, table, figure, field, stamp, ...
    source         TEXT NOT NULL,          -- text_layer | ocr | vision
    text           TEXT NOT NULL,
    bbox           REAL[],
    confidence     REAL,
    low_confidence BOOLEAN NOT NULL DEFAULT false,
    content_hash   TEXT NOT NULL,
    FOREIGN KEY (document_id, version, page) REFERENCES document_pages (document_id, version, page),
    UNIQUE (document_id, version, page, block_index)
);

-- Chunks: the unit retrieval ranks. 0004 created the table with the columns the
-- iterative-scan proof needed; the columns below are what a citation needs to point
-- at a real region of a real page. embedding becomes nullable because an index built
-- while no embedding model is installed is still searchable lexically -- recorded as
-- a degradation in the document's ingest_report, never silently.
ALTER TABLE document_chunks
    ADD COLUMN document_id     UUID REFERENCES documents(id),
    ADD COLUMN version         INTEGER,
    ADD COLUMN page            INTEGER,
    ADD COLUMN chunk_index     INTEGER,
    ADD COLUMN bbox            REAL[],
    ADD COLUMN block_ids       UUID[] NOT NULL DEFAULT '{}',
    ADD COLUMN embedding_model TEXT,
    ADD COLUMN tsv             TSVECTOR;

ALTER TABLE document_chunks ALTER COLUMN embedding DROP NOT NULL;

CREATE INDEX document_chunks_document_idx ON document_chunks (document_id, version);
CREATE INDEX document_chunks_tsv_idx ON document_chunks USING gin (tsv);
CREATE INDEX document_chunks_acl_idx ON document_chunks USING gin (acl);

-- Evidence handed to a task: the short ids (E1, E2, ... and C1, C2, ... for computed
-- values) a model cites instead of copying UUIDs, each pinned to the exact chunk,
-- document version, page and region it came from. Written by retrieval (and by the
-- calculator for computed values) at the moment the evidence is shown to the task,
-- so a citation can only ever name something the task was actually given.
CREATE TABLE task_evidence (
    task_id        UUID NOT NULL,
    evidence_id    TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('document', 'computation')),
    chunk_id       UUID,
    document_id    UUID,
    version        INTEGER,
    page           INTEGER,
    bbox           REAL[],
    text           TEXT NOT NULL,
    classification TEXT,
    detail         JSONB NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, evidence_id)
);

CREATE INDEX task_evidence_chunk_idx ON task_evidence (task_id, chunk_id);

GRANT SELECT, INSERT, UPDATE ON documents TO citadel_app;
GRANT SELECT, INSERT ON task_evidence TO citadel_app;
GRANT SELECT, INSERT ON document_pages TO citadel_app;
GRANT SELECT, INSERT ON document_blocks TO citadel_app;
