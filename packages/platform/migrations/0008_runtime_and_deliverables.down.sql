DROP INDEX IF EXISTS approvals_artifact_idx;
DROP TRIGGER IF EXISTS artifacts_freeze_released_trigger ON artifacts;
DROP FUNCTION IF EXISTS artifacts_freeze_released();
DROP TRIGGER IF EXISTS artifacts_set_updated_at ON artifacts;
DROP INDEX IF EXISTS artifacts_status_idx;
DROP INDEX IF EXISTS artifacts_task_idx;

ALTER TABLE artifacts
    DROP COLUMN updated_at,
    DROP COLUMN released_at,
    DROP COLUMN created_by,
    DROP COLUMN provenance,
    DROP COLUMN verification,
    DROP COLUMN requires_approval,
    DROP COLUMN version,
    DROP COLUMN status,
    DROP COLUMN mime_type,
    DROP COLUMN filename,
    DROP COLUMN template_id,
    DROP COLUMN title;

DROP TABLE task_memory;

DROP INDEX IF EXISTS tasks_submitted_by_idx;
DROP INDEX IF EXISTS tasks_queue_idx;

ALTER TABLE tasks
    DROP COLUMN finished_at,
    DROP COLUMN started_at,
    DROP COLUMN usage,
    DROP COLUMN worker_id,
    DROP COLUMN revision_count,
    DROP COLUMN cancel_requested,
    DROP COLUMN error,
    DROP COLUMN result,
    DROP COLUMN plan,
    DROP COLUMN primary_capability,
    DROP COLUMN requirements,
    DROP COLUMN title;

UPDATE tasks SET status = 'running' WHERE status = 'revision_required';
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
    'submitted', 'planning', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled'
));
