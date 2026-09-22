"""Empirical proof that packages/platform/migrations/0001_audit_chain.up.sql's
SCHEMA -- not any particular Python driver -- actually delivers what root AGENTS.md
invariant 8 and docs/PLAN-M0.md task 6 require: append-only, hash-chained, one
logical writer, with verify() able to catch a row tampered with directly in the
database.

Driven through `psql` (subprocess), deliberately not through psycopg: the guarantee
this file exists to prove lives in the trigger and the GRANT/REVOKE, not in
citadel_platform.audit.postgres's Python code, and proving it this way works in a
dev sandbox with no Postgres Python driver at all (root AGENTS.md's sandbox note).
This is not a recommendation to build application code this way --
citadel_platform.audit.postgres uses psycopg, as any real Postgres-backed service
should (test_audit_chain_postgres.py exercises that class directly, once a driver is
available). What this file proves is independent of which client library the
application eventually uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from citadel_platform.audit.chain import ChainRow, compute_row_hash, verify
from citadel_platform.migrations import apply_migration, discover_migrations, ensure_bootstrap
from pg_scratch import pg_scratch_db

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"

_FIELD_SEP = "\x1f"  # ASCII unit separator -- cannot collide with JSON/text payload content


def _apply_audit_chain_migration(env: dict[str, str]) -> None:
    migration = next(m for m in discover_migrations(REAL_MIGRATIONS_DIR) if m.version == "0001")
    ensure_bootstrap(env=env)
    apply_migration(migration, env=env)


def _psql(
    env: dict[str, str], sql: str, *, variables: dict[str, str] | None = None, role: str | None = None
) -> subprocess.CompletedProcess[str]:
    # -f - (stdin), not -c: confirmed empirically that psql's :'name' variable
    # substitution -- needed below so a payload's own quotes/braces never have to be
    # shell-escaped -- is applied to a script read via -f, but NOT inside a -c
    # argument (`psql -v foo=bar -c "SELECT :'foo';"` is a plain syntax error; the
    # identical text piped through -f - works). citadel_platform.migrations.runner
    # already does it this way for the same reason.
    args = ["psql", "-v", "ON_ERROR_STOP=1"]
    if role is not None:
        args += ["-U", role]
    for key, value in (variables or {}).items():
        args += ["-v", f"{key}={value}"]
    args += ["-f", "-"]
    return subprocess.run(args, input=sql, capture_output=True, text=True, env=env)


def _insert_row(env: dict[str, str], *, event_name: str, actor_id: str | None, payload_text: str, role: str | None = None) -> subprocess.CompletedProcess[str]:
    variables = {"event_name": event_name, "payload_text": payload_text}
    actor_expr = "NULL"
    if actor_id is not None:
        variables["actor_id"] = actor_id
        actor_expr = ":'actor_id'"
    sql = (
        "INSERT INTO audit_log (event_name, occurred_at_text, actor_id, payload_text) "
        f"VALUES (:'event_name', now()::text, {actor_expr}, :'payload_text');"
    )
    return _psql(env, sql, variables=variables, role=role)


def _read_chain_rows(env: dict[str, str]) -> list[ChainRow]:
    result = subprocess.run(
        [
            "psql",
            "-t",
            "-A",
            "-F",
            _FIELD_SEP,
            "-c",
            "SELECT seq, event_name, occurred_at_text, actor_id IS NULL, coalesce(actor_id, ''), "
            "payload_text, encode(prev_hash, 'hex'), encode(row_hash, 'hex') "
            "FROM audit_log ORDER BY seq;",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        seq, event_name, occurred_at_text, actor_is_null, actor_id, payload_text, prev_hex, row_hex = line.split(
            _FIELD_SEP
        )
        rows.append(
            ChainRow(
                seq=int(seq),
                event_name=event_name,
                occurred_at_text=occurred_at_text,
                actor_id=None if actor_is_null == "t" else actor_id,
                payload_text=payload_text,
                prev_hash=bytes.fromhex(prev_hex),
                row_hash=bytes.fromhex(row_hex),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# the hash chain itself matches citadel_platform.audit.chain, byte for byte
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_hash_matches_compute_row_hash_exactly():
    """The single most important cross-check in this whole design: a row the
    trigger computed and a row citadel_platform.audit.chain.compute_row_hash
    computes, from the same inputs, must be bit-for-bit identical -- otherwise a
    reader written in Python could never actually verify what Postgres wrote."""
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)

        result = _insert_row(env, event_name="policy.decision", actor_id="user-1", payload_text='{"a":1}')
        assert result.returncode == 0, result.stderr

        rows = _read_chain_rows(env)
        assert len(rows) == 1

        recomputed = compute_row_hash(
            prev_hash=rows[0].prev_hash,
            event_name=rows[0].event_name,
            occurred_at_text=rows[0].occurred_at_text,
            actor_id=rows[0].actor_id,
            payload_text=rows[0].payload_text,
        )
        assert recomputed == rows[0].row_hash


@pytest.mark.integration
def test_first_row_chains_from_the_genesis_hash():
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)
        _insert_row(env, event_name="e", actor_id=None, payload_text="{}")

        rows = _read_chain_rows(env)
        assert rows[0].prev_hash == b"\x00" * 32


# ---------------------------------------------------------------------------
# one logical writer: proven under real concurrent load, not argued
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_concurrent_writers_from_separate_processes_produce_an_unbroken_chain():
    """docs/PLAN-M0.md task 6's own "Done": concurrent writers from multiple
    processes produce an unbroken chain. Twenty separate `psql` processes, started
    together, is as close to "multiple processes, no coordination but the
    database itself" as a test gets."""
    writer_count = 20
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)

        insert_sql = (
            "INSERT INTO audit_log (event_name, occurred_at_text, actor_id, payload_text) "
            "VALUES ('concurrent.test', now()::text, 'writer-' || :'i', '{\"i\":' || :'i' || '}');"
        )
        procs = []
        for i in range(writer_count):
            # -f - (stdin), not -c: see _psql's comment -- :'i' substitution needs it.
            proc = subprocess.Popen(
                ["psql", "-v", "ON_ERROR_STOP=1", "-v", f"i={i}", "-f", "-"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(insert_sql)
            proc.stdin.close()
            procs.append(proc)

        # Not .communicate(): stdin was already written and closed above, and
        # .communicate() unconditionally tries to flush it first, raising "I/O
        # operation on closed file" -- confirmed empirically. Reading stdout/stderr
        # directly and then .wait()-ing is safe here only because a single INSERT's
        # output is a few bytes, nowhere near the OS pipe buffer size that would
        # otherwise risk a read/write deadlock.
        for proc in procs:
            assert proc.stderr is not None
            stderr = proc.stderr.read()
            returncode = proc.wait(timeout=30)
            assert returncode == 0, stderr

        rows = _read_chain_rows(env)
        assert len(rows) == writer_count
        assert [r.seq for r in rows] == list(range(1, writer_count + 1))  # gapless, strictly ordered

        result = verify(rows)
        assert result.ok is True, result.reason


# ---------------------------------------------------------------------------
# append-only: enforced for the app role by GRANT, and for everyone by trigger
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_app_role_can_select_and_insert():
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)
        result = _insert_row(env, event_name="e", actor_id=None, payload_text="{}", role="citadel_app")
        assert result.returncode == 0, result.stderr


@pytest.mark.integration
def test_app_role_cannot_update_or_delete():
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)
        _insert_row(env, event_name="e", actor_id=None, payload_text="{}")

        update_result = _psql(env, "UPDATE audit_log SET payload_text = '{}' WHERE seq = 1;", role="citadel_app")
        assert update_result.returncode != 0
        assert "permission denied" in update_result.stderr

        delete_result = _psql(env, "DELETE FROM audit_log WHERE seq = 1;", role="citadel_app")
        assert delete_result.returncode != 0
        assert "permission denied" in delete_result.stderr


@pytest.mark.integration
def test_superuser_cannot_update_or_delete_without_disabling_the_trigger():
    """Append-only holds even for a normal superuser session -- the GRANT-based
    restriction above is belt, this trigger is suspenders. Only bypassable by
    deliberately disabling the trigger first (next test), which is itself a loud,
    auditable DDL statement in Postgres's own logs."""
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)
        _insert_row(env, event_name="e", actor_id=None, payload_text="{}")

        update_result = _psql(env, "UPDATE audit_log SET payload_text = '{}' WHERE seq = 1;")
        assert update_result.returncode != 0
        assert "append-only" in update_result.stderr

        delete_result = _psql(env, "DELETE FROM audit_log WHERE seq = 1;")
        assert delete_result.returncode != 0
        assert "append-only" in delete_result.stderr


# ---------------------------------------------------------------------------
# verify() catches what the triggers couldn't prevent
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_verify_detects_a_row_tampered_with_after_disabling_the_trigger():
    """docs/PLAN-M0.md task 6's other "Done": verify() detects a row tampered with
    directly in the database. Disabling the trigger and editing a row is the
    realistic shape of that scenario -- a superuser or a determined attacker with
    direct database access, which no GRANT can stop by definition."""
    with pg_scratch_db() as env:
        _apply_audit_chain_migration(env)
        _insert_row(env, event_name="e1", actor_id="user-1", payload_text='{"a":1}')
        _insert_row(env, event_name="e2", actor_id="user-2", payload_text='{"a":2}')
        _insert_row(env, event_name="e3", actor_id="user-3", payload_text='{"a":3}')

        assert verify(_read_chain_rows(env)).ok is True  # sanity: untampered chain verifies clean

        disable = _psql(env, "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;")
        assert disable.returncode == 0, disable.stderr

        tamper = _psql(env, "UPDATE audit_log SET payload_text = '{\"a\":999}' WHERE seq = 2;")
        assert tamper.returncode == 0, tamper.stderr  # succeeds -- the trigger is disabled

        _psql(env, "ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;")

        result = verify(_read_chain_rows(env))

        assert result.ok is False
        assert result.first_break_seq == 2
        assert result.reason is not None and "edited in place" in result.reason
