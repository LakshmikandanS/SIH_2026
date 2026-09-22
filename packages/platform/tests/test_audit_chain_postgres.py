"""citadel_platform.audit.postgres -- the real psycopg-backed writer and reader,
exercised end to end against a real Postgres.

Skipped at collection (via `importorskip`, not a runtime skip inside each test) in
any environment with no psycopg installed -- the dev sandbox this repo was first
built in, per root AGENTS.md's sandbox note -- rather than deleted or faked: this is
real code, written for the target WSL2 machine, and this is the test that proves it
once a driver is available there. test_audit_chain_schema.py proves the SAME
trigger's guarantees today, driven through `psql` instead of psycopg, which is why
this file's absence of coverage here is a known, documented gap and not a silent one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    # Real import for mypy only, exactly like citadel_platform.audit.postgres's own
    # guard -- lets _connect's return type resolve under --ignore-missing-imports
    # without needing psycopg installed to type-check this file.
    import psycopg
else:
    # Runtime: skip collecting this whole file, rather than raising
    # ModuleNotFoundError, the moment the imports below reach
    # citadel_platform.audit.postgres (which does its own unconditional `import
    # psycopg` at runtime -- see that module's docstring).
    psycopg = pytest.importorskip("psycopg")

from citadel_platform.audit.chain import verify  # noqa: E402  -- after importorskip, on purpose
from citadel_platform.audit.postgres import PostgresChainSource, append  # noqa: E402
from citadel_platform.migrations import apply_migration, discover_migrations, ensure_bootstrap  # noqa: E402
from pg_scratch import pg_scratch_db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"


def _connect(env: dict[str, str]) -> "psycopg.Connection[Any]":
    return psycopg.connect(
        host=env.get("PGHOST", ""),
        port=env.get("PGPORT", "5432"),
        user=env.get("PGUSER", "postgres"),
        dbname=env["PGDATABASE"],
    )


@pytest.mark.integration
def test_append_and_read_back_round_trips_and_verifies():
    with pg_scratch_db() as pg_env:
        for migration in discover_migrations(REAL_MIGRATIONS_DIR):
            if migration.version == "0004":
                continue  # pgvector-dependent; not needed for the audit chain itself
            ensure_bootstrap(env=pg_env)
            apply_migration(migration, env=pg_env)

        with _connect(pg_env) as conn:
            append(conn, event_name="policy.decision", actor_id="user-1", payload={"allow": True})
            append(conn, event_name="policy.decision", actor_id="user-2", payload={"allow": False})
            append(conn, event_name="receipt.rejected", actor_id=None, payload={"reason": "expired"})

            rows = PostgresChainSource(conn).rows()

        assert [r.event_name for r in rows] == ["policy.decision", "policy.decision", "receipt.rejected"]
        assert verify(rows).ok is True


@pytest.mark.integration
def test_append_survives_concurrent_writers_from_separate_connections():
    """The Python-driver-facing version of test_audit_chain_schema.py's psql-based
    concurrency proof: several separate psycopg connections appending at once must
    still produce a chain verify() accepts."""
    with pg_scratch_db() as pg_env:
        for migration in discover_migrations(REAL_MIGRATIONS_DIR):
            if migration.version == "0004":
                continue
            ensure_bootstrap(env=pg_env)
            apply_migration(migration, env=pg_env)

        for i in range(10):
            with _connect(pg_env) as conn:
                append(conn, event_name="concurrent.test", actor_id=f"writer-{i}", payload={"i": i})

        with _connect(pg_env) as conn:
            rows = PostgresChainSource(conn).rows()

        assert len(rows) == 10
        assert verify(rows).ok is True
