"""packages/platform/migrations 0005 (the `role` column) and 0006 (the three demo
identities) -- proven against a real Postgres via citadel_platform.migrations, the
same pattern test_migrations_runner.py and test_audit_chain_postgres.py already use.

PLAN-M0 task 7's "Done" bar, literally: "the three seeded identities differ in
clearance and department." This file is what actually checks that, rather than
trusting the INSERT statement in 0006 by inspection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from citadel_platform.migrations import (
    apply_migration,
    discover_migrations,
    ensure_bootstrap,
    revert_migration,
)
from pg_scratch import pg_scratch_db

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"


def _apply_through(env: dict[str, str], last_version: str) -> None:
    """Every migration up to and including `last_version`, in order -- skipping
    0004 unconditionally, exactly like test_audit_chain_postgres.py's identical
    helper: 0004 needs pgvector, which this sandbox cannot install, and nothing
    about identity depends on it."""
    for migration in discover_migrations(REAL_MIGRATIONS_DIR):
        if migration.version == "0004":
            continue
        ensure_bootstrap(env=env)
        apply_migration(migration, env=env)
        if migration.version == last_version:
            return


def _query(env: dict[str, str], sql: str) -> list[list[str]]:
    result = subprocess.run(
        ["psql", "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return [line.split("|") for line in result.stdout.splitlines() if line]


@pytest.mark.integration
def test_the_three_demo_identities_differ_in_clearance_and_department():
    with pg_scratch_db() as env:
        _apply_through(env, "0006")

        rows = _query(
            env,
            "SELECT display_name, department, clearance, role, external_identity "
            "FROM users ORDER BY external_identity;",
        )

        assert len(rows) == 3
        by_identity = {r[4]: r for r in rows}
        assert set(by_identity) == {"demo-engineer-1", "demo-engineer-2", "demo-approver"}

        engineer_1 = by_identity["demo-engineer-1"]
        engineer_2 = by_identity["demo-engineer-2"]
        approver = by_identity["demo-approver"]

        # ADR-0001 §Q7 / PLAN-M0 task 7: "two engineers at different
        # classification levels in different departments, and one approver."
        assert engineer_1[3] == engineer_2[3] == "engineer"
        assert engineer_1[2] != engineer_2[2], "the two engineers must have different clearances"
        assert engineer_1[1] != engineer_2[1], "the two engineers must have different departments"
        assert approver[3] == "approver"

        # Every seeded row is a real, non-empty display name -- catches an
        # INSERT that silently transposed a column.
        for row in rows:
            assert row[0].strip() != ""


@pytest.mark.integration
def test_role_column_rejects_an_unknown_value():
    with pg_scratch_db() as env:
        _apply_through(env, "0005")

        result = subprocess.run(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "INSERT INTO users (display_name, department, clearance, role, external_identity) "
                "VALUES ('X', 'x', 'internal', 'not_a_real_role', 'x');",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "violates check constraint" in result.stderr


@pytest.mark.integration
def test_reverting_0006_then_0005_undoes_exactly_what_they_added():
    with pg_scratch_db() as env:
        migrations = {m.version: m for m in discover_migrations(REAL_MIGRATIONS_DIR)}
        _apply_through(env, "0006")
        assert _query(env, "SELECT count(*) FROM users;") == [["3"]]

        revert_migration(migrations["0006"], env=env)
        assert _query(env, "SELECT count(*) FROM users;") == [["0"]], (
            "0006's down must remove exactly the three seeded rows, leaving the "
            "(now empty) users table -- not drop the table or leave stragglers"
        )

        revert_migration(migrations["0005"], env=env)
        remaining = _query(
            env,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'role';",
        )
        assert remaining == [], "0005's down must drop the role column"
