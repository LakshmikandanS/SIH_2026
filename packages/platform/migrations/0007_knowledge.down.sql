DROP TABLE IF EXISTS task_evidence;
DROP INDEX IF EXISTS document_chunks_acl_idx;
DROP INDEX IF EXISTS document_chunks_tsv_idx;
DROP INDEX IF EXISTS document_chunks_document_idx;

-- Chunks written by 0007-era ingestion reference documents; they cannot outlive them.
DELETE FROM document_chunks WHERE document_id IS NOT NULL;
UPDATE document_chunks SET embedding = array_fill(0::real, ARRAY[768])::vector WHERE embedding IS NULL;
ALTER TABLE document_chunks ALTER COLUMN embedding SET NOT NULL;

ALTER TABLE document_chunks
    DROP COLUMN tsv,
    DROP COLUMN embedding_model,
    DROP COLUMN block_ids,
    DROP COLUMN bbox,
    DROP COLUMN chunk_index,
    DROP COLUMN page,
    DROP COLUMN version,
    DROP COLUMN document_id;

DROP TABLE document_blocks;
DROP TABLE document_pages;
DROP TABLE documents;
