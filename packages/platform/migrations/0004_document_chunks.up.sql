-- 0004_document_chunks -- exists now, with nothing to retrieve yet, purely so
-- hnsw.iterative_scan can be proven correct before M2 needs it for real
-- (docs/PLAN-M0.md task 5): "a filtered vector query that excludes most of the
-- corpus must still return k rows... invisible until it bites." Ingestion and
-- retrieval themselves are M2 work, explicitly listed under PLAN-M0.md's "Not in
-- M0" -- this migration is schema and configuration only.
--
-- Requires the pgvector extension, which -- unlike pgcrypto in migration 0001, a
-- bundled contrib module -- does NOT ship with the base postgresql-16 package. It
-- is a separate install (the pgvector/pgvector Docker image, or an OS package where
-- one exists) that the real machine's Compose stack (ops/compose/) provides and
-- this migration assumes. If `CREATE EXTENSION vector` fails with "extension
-- \"vector\" is not available", that is a missing install on whatever machine ran
-- this, not a bug in this file -- see root AGENTS.md's sandbox note: the dev
-- sandbox this repo was first built in has no path to install it (no network to
-- fetch it, no server-dev headers to build it from source), which is why this
-- migration's own verification is by review here, not by execution -- see
-- packages/platform/tests/test_vector_iterative_scan.py's skip reason.
CREATE EXTENSION IF NOT EXISTS vector;

-- 768 = nomic-embed-text's native output size (registry/models.demo-local.yaml's
-- embed-text candidate tag, read for this comment only). A vector column's width is
-- a storage fact, not a routing choice: switching to an embedding model with a
-- different native dimension needs a new migration regardless of what registry/
-- says, the same way changing a column's data type would. That is not the same
-- thing as hardcoding model identity in code (root AGENTS.md invariant 1) -- this
-- migration names no model, only a number chosen because of one.
CREATE TABLE document_chunks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential')),
    acl            TEXT[] NOT NULL DEFAULT '{}',  -- citadel_contracts.receipts.ResourceLike.acl shape: identities allowed to see this chunk
    content        TEXT NOT NULL,
    embedding      VECTOR(768) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX document_chunks_embedding_hnsw ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Root AGENTS.md invariant 11 / ADR-0003 / packages/platform/AGENTS.md: without
-- this, a filtered HNSW query can silently return fewer than k rows once the filter
-- (here, classification/ACL -- invariant 5's "authorization filters before
-- ranking") excludes most of the index, because HNSW's graph search can exhaust its
-- candidate list before finding k matches. `strict_order` re-scans until it actually
-- finds k or exhausts the table, trading latency for correctness -- the right trade
-- for an ACL filter, which is restrictive by design. Set per-database via dynamic
-- SQL (not a literal `ALTER DATABASE citadel SET ...`) so this migration works
-- against whatever database it is actually run against, not only one named
-- "citadel".
-- Edited in place (2026-09-23), not superseded by a later migration, deliberately: this
-- line originally said 'strict', which pgvector has never accepted -- its values are
-- off / relaxed_order / strict_order -- so this migration could not apply against ANY
-- real pgvector ("invalid value for parameter hnsw.iterative_scan"). It had only ever
-- been reviewed, never run, because the sandbox that wrote it had no pgvector; the first
-- real pgvector build (0.8.1, compiled for the sandbox) found it immediately. A migration
-- that no database has ever successfully applied has no applied state to preserve.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET hnsw.iterative_scan = %L', current_database(), 'strict_order');
END
$$;

GRANT SELECT, INSERT ON document_chunks TO citadel_app;
