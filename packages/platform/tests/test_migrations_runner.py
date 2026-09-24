"""citadel_platform.migrations -- discovery, ordering, and actually applying and
reverting real SQL. Two kinds of test: pure logic (discover_migrations, pending,
to_revert -- no subprocess, no database, run everywhere) and a real end-to-end run
against whatever Postgres pg_scratch.pg_scratch_db provides, skipped with a clear
reason if none is reachable. The end-to-end test is what actually proves
docs/PLAN-M0.md task 5's "migrations run forward and back" -- the pure-logic tests
below prove the *ordering* is right, not that the SQL itself is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from citadel_platform.migrations import (
    Migration,
    MigrationError,
    apply_migration,
    applied_versions,
    discover_migrations,
    ensure_bootstrap,
    pending,
    revert_migration,
    to_revert,
)
from pg_scratch import pg_scratch_db, pgvector_available

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _table_names(env: dict[str, str]) -> set[str]:
    result = subprocess.run(
        ["psql", "-t", "-A", "-c", "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return set(result.stdout.split())


# ---------------------------------------------------------------------------
# pure logic -- no subprocess, no database
# ---------------------------------------------------------------------------


def test_discovers_the_real_migrations_in_version_order():
    migrations = discover_migrations(REAL_MIGRATIONS_DIR)
    assert [m.version for m in migrations] == [f"{n:04d}" for n in range(1, 11)]
    assert migrations[0].name == "audit_chain"


def test_discover_pairs_up_and_down_by_version(tmp_path):
    _write(tmp_path / "0001_first.up.sql", "-- up")
    _write(tmp_path / "0001_first.down.sql", "-- down")
    _write(tmp_path / "0002_second.up.sql", "-- up")
    _write(tmp_path / "0002_second.down.sql", "-- down")

    migrations = discover_migrations(tmp_path)

    assert [m.version for m in migrations] == ["0001", "0002"]
    assert migrations[0].name == "first"
    assert migrations[1].up_path.name == "0002_second.up.sql"


def test_discover_raises_on_an_up_file_with_no_down_file(tmp_path):
    _write(tmp_path / "0001_first.up.sql", "-- up")

    with pytest.raises(MigrationError, match=r"no matching \.down\.sql"):
        discover_migrations(tmp_path)


def test_discover_raises_on_a_duplicate_version(tmp_path):
    _write(tmp_path / "0001_first.up.sql", "-- up")
    _write(tmp_path / "0001_first.down.sql", "-- down")
    _write(tmp_path / "0001_again.up.sql", "-- up")
    _write(tmp_path / "0001_again.down.sql", "-- down")

    with pytest.raises(MigrationError, match="duplicate migration version '0001'"):
        discover_migrations(tmp_path)


def test_discover_raises_on_a_malformed_filename(tmp_path):
    _write(tmp_path / "not_a_migration.sql", "-- ?")

    with pytest.raises(MigrationError, match="does not match"):
        discover_migrations(tmp_path)


def _migration(version: str) -> Migration:
    # pending()/to_revert() are pure set-and-sort logic over Migration.version --
    # they never touch up_path/down_path, so fake paths are fine here.
    return Migration(
        version=version, name=f"m{version}", up_path=Path(f"{version}.up.sql"), down_path=Path(f"{version}.down.sql")
    )


def test_pending_returns_unapplied_in_version_order():
    migrations = [_migration("0001"), _migration("0002"), _migration("0003")]
    assert [m.version for m in pending(migrations, {"0001"})] == ["0002", "0003"]


def test_pending_is_empty_when_everything_is_applied():
    migrations = [_migration("0001"), _migration("0002")]
    assert pending(migrations, {"0001", "0002"}) == []


def test_to_revert_returns_applied_in_reverse_version_order():
    migrations = [_migration("0001"), _migration("0002"), _migration("0003")]
    reverted = to_revert(migrations, {"0001", "0002", "0003"}, steps=2)
    assert [m.version for m in reverted] == ["0003", "0002"]


def test_to_revert_only_considers_applied_migrations():
    migrations = [_migration("0001"), _migration("0002")]
    reverted = to_revert(migrations, {"0001"}, steps=5)
    assert [m.version for m in reverted] == ["0001"]


# ---------------------------------------------------------------------------
# real end to end -- needs a reachable Postgres; see conftest.py's pg_env fixture
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_real_migrations_apply_forward_and_back():
    """docs/PLAN-M0.md task 5's own "Done": migrations run forward and back.

    Only through 0003: migration 0004 needs the pgvector extension, which this
    sandbox cannot install (root AGENTS.md's sandbox note; migration 0004's own
    header comment). test_vector_iterative_scan.py covers 0004 on its own terms,
    skipped with that specific reason rather than folded silently into this test.
    """
    migrations = discover_migrations(REAL_MIGRATIONS_DIR)
    # Every migration when pgvector is installed (0004 creates the vector column and
    # 0007 builds on it); only the pgvector-free prefix where it is not.
    expected_tables: tuple[str, ...]
    if pgvector_available():
        up_to_0003 = list(migrations)
        expected_tables = (
            "audit_log", "users", "tasks", "task_journal", "artifacts", "approvals",
            "model_runtime_state", "document_chunks", "documents", "document_pages",
            "document_blocks", "task_memory", "egress_events", "trace_spans",
        )
    else:
        up_to_0003 = [m for m in migrations if m.version <= "0003"]
        expected_tables = ("audit_log", "users", "tasks", "task_journal", "artifacts", "approvals", "model_runtime_state")

    with pg_scratch_db() as pg_env:
        ensure_bootstrap(env=pg_env)
        assert applied_versions(env=pg_env) == set()

        for migration in up_to_0003:
            apply_migration(migration, env=pg_env)
        assert applied_versions(env=pg_env) == {m.version for m in up_to_0003}

        tables = _table_names(pg_env)
        for expected in expected_tables:
            assert expected in tables, f"{expected} missing after applying migrations forward"

        for migration in reversed(up_to_0003):
            revert_migration(migration, env=pg_env)
        assert applied_versions(env=pg_env) == set()

        tables = _table_names(pg_env)
        for gone in expected_tables:
            assert gone not in tables, f"{gone} still present after reverting migrations"


@pytest.mark.integration
def test_a_second_up_run_is_a_no_op():
    """Applying migrations twice in a row must not re-run or fail the second time --
    the whole point of tracking applied_versions."""
    migrations = discover_migrations(REAL_MIGRATIONS_DIR)
    up_to_0003 = [m for m in migrations if m.version <= "0003"]

    with pg_scratch_db() as pg_env:
        ensure_bootstrap(env=pg_env)
        for migration in up_to_0003:
            apply_migration(migration, env=pg_env)

        assert pending(up_to_0003, applied_versions(env=pg_env)) == []
