DROP TABLE IF EXISTS service_heartbeats;
DROP INDEX IF EXISTS memory_events_task_idx;
DROP INDEX IF EXISTS memory_events_time_idx;
DROP TABLE IF EXISTS memory_events;
DROP TRIGGER IF EXISTS memories_set_updated_at ON memories;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS document_versions;
DROP INDEX IF EXISTS documents_folder_idx;
ALTER TABLE documents DROP COLUMN IF EXISTS folder;
DROP TRIGGER IF EXISTS task_drafts_set_updated_at ON task_drafts;
DROP TABLE IF EXISTS task_drafts;
DROP TABLE IF EXISTS task_shared_state;
DROP TRIGGER IF EXISTS task_agents_set_updated_at ON task_agents;
DROP TABLE IF EXISTS task_agents;
DROP INDEX IF EXISTS task_journal_agent_idx;
ALTER TABLE task_journal DROP COLUMN IF EXISTS agent_id;
ALTER TABLE tasks
    DROP COLUMN IF EXISTS draft_id,
    DROP COLUMN IF EXISTS pause_requested,
    DROP COLUMN IF EXISTS kind;
UPDATE tasks SET status = 'failed', error = coalesce(error, 'paused when migration 0010 was reverted')
    WHERE status = 'paused';
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
    'submitted', 'planning', 'running', 'revision_required',
    'awaiting_approval', 'completed', 'failed', 'cancelled'
));
