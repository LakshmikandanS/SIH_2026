-- 0001_audit_chain -- the audit chain (root AGENTS.md invariant 8;
-- packages/platform/AGENTS.md "The audit chain"): append-only, hash-chained,
-- exactly one logical writer. All three properties live here, in the schema, not in
-- application discipline, so "one logical writer" holds across multiple worker
-- processes and survives a restart -- a threading.Lock (the prototype's mistake)
-- cannot do either.
--
-- pgcrypto is a bundled Postgres contrib extension: it ships with the postgresql-16
-- package itself, no network fetch needed, unlike pgvector (see migration 0004).
-- digest() -- SHA-256 -- comes from it. gen_random_uuid(), used from migration 0002
-- on, is a Postgres 13+ builtin and needs no extension.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- The application connects as this role, never as a superuser. Created here, with
-- the schema whose access it exists to restrict, rather than in a separate ops/
-- provisioning step that could drift out of sync with what the schema actually
-- needs. LOGIN with no password: how this role authenticates (client cert, SCRAM
-- password, peer auth) is a deployment-time concern for ops/compose/, not a schema
-- concern -- see that directory for the real connection setup.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'citadel_app') THEN
        CREATE ROLE citadel_app LOGIN;
    END IF;
END
$$;

-- occurred_at_text / payload_text hold the *exact* bytes the hash below covers,
-- verbatim, as produced once by the application (citadel_platform.audit.postgres.
-- format_occurred_at / canonical_json). occurred_at / payload are derived from them
-- by audit_log_chain() below, purely for querying (an index, a `payload ->> 'foo'`)
-- -- never themselves hashed. The split exists because neither a TIMESTAMPTZ's
-- `::text` cast nor a JSONB's `::text` cast has a format this migration wants to
-- promise byte-for-byte forever (locale, Postgres version, key order are all its
-- business, not ours); a plain TEXT column the application already fully controls
-- has no such ambiguity, and citadel_platform.audit.chain.compute_row_hash
-- reproduces it exactly because both sides are hashing the same stored string, not
-- each re-deriving their own serialization of the same logical value.
--
-- occurred_at/payload are NOT `GENERATED ALWAYS AS (...) STORED` columns: Postgres
-- refuses a generated-column expression it cannot prove IMMUTABLE, and a
-- text-to-timestamptz cast is only STABLE (its result depends on the session's
-- TimeZone setting when the text has no explicit offset, even though
-- format_occurred_at always supplies one) -- confirmed empirically, not assumed:
-- `CREATE TABLE ... GENERATED ALWAYS AS (occurred_at_text::timestamptz)` fails
-- outright with "generation expression is not immutable". audit_log_chain() sets
-- both columns itself, in the same BEFORE INSERT pass that computes the hash, which
-- has no such restriction.
-- seq is a plain BIGINT, assigned by audit_log_chain() itself under the advisory
-- lock below -- deliberately NOT `GENERATED ALWAYS AS IDENTITY`. Confirmed
-- empirically, not assumed: an identity column's sequence value is assigned when
-- Postgres computes column defaults, which happens *before* a BEFORE ROW trigger
-- runs -- and a sequence's whole point is to hand out values to concurrent
-- transactions without blocking any of them on each other. Two concurrent inserts
-- can each grab a seq (say 5 and 6) in one order and then race for the advisory
-- lock in the *other* order, so the transaction holding seq=6 can commit first --
-- seq order and true hash-chain order silently disagree the moment two writers
-- overlap. A hand-rolled `MAX(seq)+1`, computed after the lock is held, ties seq
-- assignment to the exact same serialized read that determines prev_hash, so the
-- two can never disagree. This is the opposite of what an identity/sequence column
-- is *for* (high-concurrency, gap-tolerant) -- which is exactly why it's wrong for
-- a structure that must be gapless and strictly ordered.
CREATE TABLE audit_log (
    seq              BIGINT PRIMARY KEY,
    event_name       TEXT NOT NULL,                 -- a name from citadel_contracts's open event registry -- never a CHECK'd closed set (AGENTS.md "The closed vocabulary")
    occurred_at_text TEXT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL,
    actor_id         TEXT,                          -- NULL for a system-originated event; never a caller-supplied identity (test_identity_not_from_body)
    payload_text     TEXT NOT NULL,
    payload          JSONB NOT NULL,
    prev_hash        BYTEA NOT NULL,
    row_hash         BYTEA NOT NULL,
    CONSTRAINT audit_log_hash_length CHECK (octet_length(prev_hash) = 32 AND octet_length(row_hash) = 32)
);

COMMENT ON TABLE audit_log IS
    'The legal record: decisions, denials, approvals, releases. Append-only, '
    'hash-chained, one logical writer -- see audit_log_chain() below. Never sampled, '
    'never truncated (root AGENTS.md invariant 8). Not the execution trace and not '
    'metrics -- packages/platform/AGENTS.md''s three-record-types table.';

CREATE INDEX audit_log_occurred_at_idx ON audit_log (occurred_at);
CREATE INDEX audit_log_event_name_idx ON audit_log (event_name);
CREATE INDEX audit_log_payload_gin_idx ON audit_log USING gin (payload);

-- One logical writer, enforced here rather than trusted to application discipline:
-- every insert takes a session-wide advisory lock before it reads the previous
-- row's hash, so two concurrent transactions -- different processes, different
-- machines even -- can never both read the same "previous" row and race to append.
-- The second necessarily blocks until the first commits (releasing the lock) and
-- then sees the first's row as its own predecessor. hashtext() of a fixed string
-- gives a stable, arbitrary lock key; the value doesn't matter, only that every
-- writer agrees on it. Proven under real concurrent load, not just argued, in
-- packages/platform/tests/test_audit_chain_schema.py.
CREATE OR REPLACE FUNCTION audit_log_chain() RETURNS trigger AS $$
DECLARE
    prev     BYTEA;
    last_seq BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('citadel.audit_log'));

    SELECT seq, row_hash INTO last_seq, prev FROM audit_log ORDER BY seq DESC LIMIT 1;
    IF prev IS NULL THEN
        last_seq := 0;
        prev := decode(repeat('00', 32), 'hex');  -- genesis predecessor: 32 zero bytes
    END IF;

    NEW.seq := last_seq + 1;  -- any caller-supplied seq is ignored, same as prev_hash/row_hash below
    NEW.occurred_at := NEW.occurred_at_text::timestamptz;
    NEW.payload := NEW.payload_text::jsonb;
    NEW.prev_hash := prev;
    NEW.row_hash := digest(
        prev
        || convert_to(NEW.event_name, 'UTF8')
        || convert_to(NEW.occurred_at_text, 'UTF8')
        || convert_to(COALESCE(NEW.actor_id, ''), 'UTF8')
        || convert_to(NEW.payload_text, 'UTF8'),
        'sha256'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_chain_trigger
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

-- Append-only: reject UPDATE/DELETE outright for any role, including a superuser
-- acting normally. A superuser *can* still `ALTER TABLE ... DISABLE TRIGGER` to get
-- around this -- that is exactly the scenario citadel_platform.audit.chain.verify()
-- exists to catch, and packages/platform/tests/test_audit_chain_schema.py proves it
-- does, by doing precisely that and then verifying.
CREATE OR REPLACE FUNCTION audit_log_no_tamper() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_tamper();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_tamper();

-- Belt and suspenders even before the triggers: the application role's grants never
-- included UPDATE or DELETE in the first place.
GRANT SELECT, INSERT ON audit_log TO citadel_app;
