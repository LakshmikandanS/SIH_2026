-- Reverts 0001_audit_chain.up.sql.
--
-- Deliberately does NOT `DROP ROLE citadel_app` or `DROP EXTENSION pgcrypto`: later
-- migrations' tables also GRANT to citadel_app and may also need pgcrypto's
-- digest()/gen_random_uuid()-adjacent functions. Down-migrations are meant to be
-- applied in reverse order, one at a time (citadel_platform.migrations.runner), so by
-- the time this one runs, every later migration's own down-script should already have
-- revoked its own grants -- but relying on that ordering to make `DROP ROLE` safe
-- here is exactly the kind of cross-migration coupling worth avoiding. Both are
-- cluster/database-wide and effectively free to leave behind; the role is inert
-- (no table left to touch) once every table that granted to it is gone.
DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
DROP TRIGGER IF EXISTS audit_log_chain_trigger ON audit_log;
DROP FUNCTION IF EXISTS audit_log_no_tamper();
DROP FUNCTION IF EXISTS audit_log_chain();
DROP TABLE IF EXISTS audit_log;
