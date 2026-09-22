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

## Session identity

`citadel_contracts.identity` is a pure sign/verify primitive that takes a key and never
decides where one comes from. `citadel_platform.identity` (PLAN-M0 task 7) is where that
key actually lives: `keys.py` reads a base64-encoded 32-byte Ed25519 private key from
`CITADEL_SESSION_SIGNING_KEY`, or — unlike this package's general "fail closed" posture —
falls back to a fresh, per-process, cryptographically random key with a loud
`warnings.warn`. That module's own docstring explains why fail-loud beats fail-closed
here: a demo box that refuses to boot without an operator minting a key by hand is a
worse M0 failure than a working demo whose sessions do not survive a restart, provided
the fallback can never be predicted or influenced by an attacker. `python -m
citadel_platform.identity genkey` is how an operator produces a value for that variable.
Migrations 0005/0006 (the `role` column and the three ADR-0001 §Q7 demo identities) are
the Postgres side of the same task; the `role` column is deliberately singular even
though `citadel_contracts.domain.User.roles` is a tuple, because `registry/policy.yaml`
branches on a singular `actor.role` — see migration 0005's own header comment.

## `python -m citadel_platform.<pkg>` needs its own `__main__.py`

Every CLI-shaped subpackage here (`migrations`, `identity`) is documented and invoked as
`python -m citadel_platform.<pkg>`. That invocation needs a `<pkg>/__main__.py`
delegating to `<pkg>/cli.py`'s `main()` — a package with a `cli.py` and an
`if __name__ == "__main__"` guard is still not runnable via `-m` without one. Missing it
fails with `No module named citadel_platform.<pkg>.__main__`, not an error inside
`cli.py`, and nothing in `scripts/check.sh` catches it: pytest never shells out to the
CLI as `python -m`. `migrations` shipped without one from task 5 until this was found by
manually smoke-testing `identity`'s CLI the same way. Add the same two-line file for any
new CLI subpackage; do not assume `cli.py` alone is enough.

## `classification_ceiling` is uppercased once, at the boundary — now actually true

`_known_classification()` (`registry/schema.py`) validates a registry value via
`Classification.rank(value.upper())` but was returning the original `value` — so every
`ToolEntry`/`ModelEntry`/`Profile.classification_ceiling` kept the YAML file's as-written
lowercase casing (`"confidential"`, `"public"`) rather than becoming the lattice's
uppercase form, contradicting both this function's own docstring and
`classification.py`'s module docstring, which both name this exact function as the
boundary where that normalisation happens. Nothing had ever called
`Classification.rank()`/`.exceeds()` on a registry-sourced classification value before
`citadel_tools.policy`'s `exceeds` operator did (PLAN-M0 task 8) — eight of that module's
own tests are what caught it, all failing the same way: `ValueError: unknown
classification 'confidential'`. Fixed by uppercasing before returning, not only before
validating; `test_registry_loader.py`'s assertions that had pinned the old, wrong
lowercase values now pin the corrected uppercase ones — a registry-shaped version of
this file's own `restricted` bug above, caught the same way, by a real first consumer
rather than by inspection. If anything ever reads a registry classification value some
way other than through the typed `ClassificationLevel` field, it inherits this same
uppercasing obligation; prefer the typed field instead.
