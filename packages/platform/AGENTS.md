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

## The same `psql` substitution, extended to reading and writing rows

`citadel_platform.migrations.runner` was the first place this package worked around
having no installable `psycopg` in this sandbox; `citadel_platform.audit.psql_client`
and `citadel_platform.identity.store` are the same substitution applied to the two other
places this package talks to Postgres — a second `ChainSource` (`PsqlChainSource`) plus
`append_via_psql`, and `list_users`/`get_user_by_external_identity`. Both exist because
`services/api` (the M0 checkpoint, root AGENTS.md's "Current state") needed a real login
and a real audit write that work in this sandbox, not only on the real machine.

`_psql.py` (leading underscore: private to this package) is the one shared runner both
need: `run_psql_csv` pipes one statement to `psql --csv -q -v ON_ERROR_STOP=1 -f -`
(script mode via stdin, not a `-c` argument — `:'name'` safe-quoting substitution is a
script/meta-command feature and is not applied to `-c`, confirmed empirically by a
literal `syntax error at or near ":"` before this was found) and parses the result with
`csv.reader`, not `-t -A`, because `payload_text`/`display_name` can contain commas,
quotes and newlines that naive delimiter-splitting would corrupt. `-q` suppresses a
trailing command-completion line (`INSERT 0 1`) that `-f -` mode otherwise appends after
the `--csv` output — also confirmed empirically, by a parse that briefly treated it as a
spurious data row. Deliberately not shared with `migrations.runner`'s own private
`_run_psql`: that one is shaped around running whole *scripts* with
`--single-transaction`, this one around one statement with a result set — different
subsystems that happen to reach for the same tool, not a dependency either should have
on the other.

`audit.psql_client`'s `_canonical_json`/`_format_occurred_at` are intentionally
duplicated from `audit.postgres`'s copies rather than imported — importing that module
here fails at its `import psycopg` line before either function would be reachable. Both
copies must stay byte-identical (they define the exact text
`audit.chain.compute_row_hash` hashes); `test_psql_client_matches_postgres_formatting`
pins the agreement so a future edit to one cannot silently drift from the other.
`identity.store`'s `User.user_id` reads from `external_identity`, never the row's UUID
`id` — migration 0002's own comment calls `external_identity` "the subject claim from a
verified session token," which is exactly what `citadel_contracts.identity`'s token
claims put in `sub`; using the UUID instead would mean a token's `sub` and the row it
came from disagree about which field *is* the identity.

Both are proven against a real Postgres via `pg_scratch_db()`, not mocked:
`test_psql_client.py` (round-trip with special characters in the payload, a `NULL`
actor_id staying `None` rather than becoming `""`, a 5-row chain that `audit.chain.
verify()` accepts, an empty table, the formatting pin above) and
`test_identity_store.py` (the three real seeded identities round-trip with the right
roles/clearance/department, a lookup miss returns `None` rather than raising, an empty
table returns `[]`).

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
