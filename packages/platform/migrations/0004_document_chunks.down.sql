-- Reverts 0004_document_chunks.up.sql.
DROP INDEX IF EXISTS document_chunks_embedding_hnsw;
DROP TABLE IF EXISTS document_chunks;
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I RESET hnsw.iterative_scan', current_database());
END
$$;
-- Deliberately does not DROP EXTENSION vector: harmless to leave installed, and
-- dropping it would fail anyway if any other object in the database still depended
-- on it (none does after the DROP TABLE above -- that's incidental, not a reason to
-- couple the two).
