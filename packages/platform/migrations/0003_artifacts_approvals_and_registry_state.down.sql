-- Reverts 0003_artifacts_approvals_and_registry_state.up.sql.
DROP TRIGGER IF EXISTS model_runtime_state_set_updated_at ON model_runtime_state;
DROP TABLE IF EXISTS model_runtime_state;
DROP TABLE IF EXISTS approvals;
DROP TABLE IF EXISTS artifacts;
