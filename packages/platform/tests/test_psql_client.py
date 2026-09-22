"""citadel_platform.audit.psql_client -- the psql-subprocess ChainSource/append,
proven against a real Postgres exactly like the psycopg one is proven in
test_audit_chain_schema.py, just reachable without a driver.

Marked `integration` (needs Postgres) like every other test in this file's
family; `pg_scratch_db()` skips cleanly when none is reachable rather than
failing red -- see that fixture's own docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_platform.audit.chain import verify
from citadel_platform.audit.psql_client import PsqlChainSource, append_via_psql
from citadel_platform.migrations import apply_migration, discover_migrations, ensure_bootstrap
from pg_scratch import pg_scratch_db

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"

pytestmark = pytest.mark.integration


def _apply(env: dict[str, str], version: str) -> None:
    ensure_bootstrap(env=env)
    migration = next(m for m in discover_migrations(REAL_MIGRATIONS_DIR) if m.version == version)
    apply_migration(migration, env=env)


def test_append_then_rows_round_trips_every_field():
    with pg_scratch_db() as env:
        _apply(env, "0001")

        seq = append_via_psql(
            env,
            event_name="policy.decision",
            actor_id="demo-engineer-1",
            payload={"tool": "docs.search", "decision": "allow", "note": 'has a "quote", a comma, and a newline\nhere'},
        )
        assert seq == 1

        rows = PsqlChainSource(env=env).rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.seq == 1
        assert row.event_name == "policy.decision"
        assert row.actor_id == "demo-engineer-1"
        assert "quote" in row.payload_text
        assert len(row.prev_hash) == 32
        assert len(row.row_hash) == 32


def test_a_null_actor_id_round_trips_as_none_not_empty_string():
    with pg_scratch_db() as env:
        _apply(env, "0001")

        append_via_psql(env, event_name="system.startup", actor_id=None, payload={})

        rows = PsqlChainSource(env=env).rows()
        assert rows[0].actor_id is None


def test_multiple_appends_produce_a_chain_that_verifies():
    with pg_scratch_db() as env:
        _apply(env, "0001")

        for i in range(5):
            append_via_psql(
                env, event_name="policy.decision", actor_id=f"actor-{i}", payload={"i": i}
            )

        rows = PsqlChainSource(env=env).rows()
        assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
        result = verify(rows)
        assert result.ok is True


def test_an_empty_audit_log_returns_no_rows():
    with pg_scratch_db() as env:
        _apply(env, "0001")
        assert PsqlChainSource(env=env).rows() == []


def test_psql_client_matches_postgres_formatting():
    """`_canonical_json`/`_format_occurred_at` here must stay byte-identical
    to citadel_platform.audit.postgres's copies -- both define the exact
    text compute_row_hash hashes. Reaching into psql_client's private copies
    directly (not importing postgres.py, which fails to import in this
    sandbox) and checking them against the same fixed cases postgres.py's
    own module docstring format promises: sorted keys, no extra whitespace,
    an explicit UTC offset.
    """
    from datetime import datetime, timezone

    from citadel_platform.audit.psql_client import _canonical_json, _format_occurred_at

    assert _canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert _format_occurred_at(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        _format_occurred_at(datetime(2026, 1, 1))  # naive -- must be rejected, not silently assumed UTC
