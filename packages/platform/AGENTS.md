# AGENTS.md — platform

Everything cross-cutting that touches infrastructure: Postgres, migrations, the audit chain
writer, config and registry loading, tracing and metrics.

## Depends on

`contracts` only.

## Three record types — never conflate them

The prototype merged these and it cost it clarity. They have different retention, different
guarantees and different write paths.

| | Purpose | Properties |
|---|---|---|
| **Audit chain** | The legal record: decisions, denials, approvals, releases | Append-only, hash-chained, exactly one logical writer, **never sampled**, retained indefinitely |
| **Execution trace** | The operational record: spans for plans, steps, tool and model calls | Structured, sampleable, retained by policy, queryable |
| **Metrics** | Aggregate health: latency, tokens, VRAM, queue depth, errors | Time series, cheap, dashboarded |

If you are tempted to write an audit event and a trace span from the same call site with
the same payload, that is the conflation. They answer different questions to different
readers.

## The audit chain

Hash-chained, append-only, **one logical writer**. The prototype got the shape right and
the concurrency wrong (a `threading.Lock` around SQLite). Here it is Postgres:

- A single writer is a property of the *transaction design*, not a mutex. Serialise on a
  row lock or a sequence, inside the database, so it survives multiple workers and a
  restart.
- `verify()` recomputes the whole chain and is exposed to the UI (handoff §8.4). A chain
  that cannot be re-verified on demand is not evidence.
- Denials are audit events. A denial that is not recorded did not happen, as far as an
  auditor is concerned.

## Registry loading

Registries are data (`registry/*.yaml`), selected by `CITADEL_PROFILE`. This package loads
and validates them and hands typed objects to whoever asked. Validation is strict: an
unknown field is an error, not a warning. A registry that silently ignores a typo will cost
someone a day.

## Postgres

`pgvector` for the vector index. **Enable `hnsw.iterative_scan`** — without it a filtered
HNSW query silently returns fewer rows than requested, and ACL filters are restrictive by
design. `tests/test_vector_iterative_scan.py` (marked `integration`, skipped where
pgvector isn't installed) is the test for this; do not delete it because it looks
redundant.

## Migrations and the audit chain, concretely

`migrations/NNNN_name.{up,down}.sql`, applied and reverted by `citadel_platform.migrations`
— a `psql`-subprocess runner, deliberately not psycopg (see its module docstring), so it
applies migrations for real even with no Postgres Python driver installed. The hash chain
itself is `citadel_platform.audit.chain` (pure Python, no database) plus
`citadel_platform.audit.postgres` (the real psycopg writer/reader); the "one logical
writer" guarantee in the table above lives in migration 0001's `BEFORE INSERT` trigger, not
in either Python module, and is proven against a real Postgres in
`tests/test_audit_chain_schema.py`. Read that file before changing the trigger — it is
what catches a regression in the concurrency guarantee, not a unit test of Python code.

This package's tests need a reachable Postgres: `scripts/dev-db.sh start` (repo root)
brings up a disposable one and prints the `PGHOST`/`PGPORT`/`PGUSER` to export. Without
them, the integration tests and schema proofs skip cleanly instead of failing.
