-- Reverts 0002_identity_and_tasks.up.sql.
DROP TRIGGER IF EXISTS tasks_set_updated_at ON tasks;
DROP FUNCTION IF EXISTS set_updated_at();
DROP TABLE IF EXISTS task_journal;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS users;
