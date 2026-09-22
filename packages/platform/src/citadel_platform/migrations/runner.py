"""Applies and reverts packages/platform/migrations/*.sql.

"Migrations from the first commit -- never a hand-edited schema" (docs/PLAN-M0.md
task 5). This module is the thing that makes that a checkable fact rather than a
promise: it discovers `NNNN_name.up.sql` / `NNNN_name.down.sql` pairs, tracks which
have been applied in a `schema_migrations` table, and applies or reverts them one at
a time, each inside its own transaction.

Deliberately built on `psql` (via `subprocess`), not `psycopg`: `psql` is the one
Postgres client guaranteed to exist anywhere `postgres` itself does -- installing it
needs no network, no compiler, nothing beyond the `postgresql-client` package that
ships with the server (root AGENTS.md's sandbox note) -- so migrations can be
applied and reverted for real in a network-restricted dev sandbox that has no
Python Postgres driver at all, using the exact same code path the real WSL2 machine
uses. `citadel_platform.audit.postgres`'s writer is psycopg-based, as any real
Postgres-backed application code should be; this module isn't application code, it's
closer to `ops/` tooling that happens to live next to the schema it manages, and
running one `psql` subprocess per migration is not a cost that matters at the
frequency migrations run.

Connection info comes entirely from the standard `PG*` environment variables
(`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, ...) that both `psql` and
`psycopg` read natively -- one connection configuration format for the whole
package, not a second one invented for this module.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z][a-z0-9_]*)\.(?P<direction>up|down)\.sql$")

_BOOTSTRAP_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(Exception):
    """Anything that stops migrations from being applied or reverted: a malformed
    filename, an up file with no matching down file, a duplicate version, or `psql`
    itself failing. Always carries enough detail to act on -- a bare `psql` stderr
    dump or a bare filename, never both silently swallowed into one message."""


@dataclass(frozen=True)
class Migration:
    """One `NNNN_name` migration: its version (the sortable, zero-padded string --
    kept as text, not int, so a version is never accidentally used as a row count or
    an offset), its name (for logging only), and the two files that apply/revert it.
    """

    version: str
    name: str
    up_path: Path
    down_path: Path


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Every `NNNN_name.up.sql` under `migrations_dir`, paired with its
    `NNNN_name.down.sql`, sorted by version. Raises `MigrationError` -- rather than
    silently skipping -- on anything that would make "forward and back" a false
    promise: a filename that doesn't match the pattern, two up files claiming the
    same version, or an up file with no down file at all.
    """
    ups: dict[str, tuple[str, Path]] = {}
    downs: dict[str, Path] = {}

    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: does not match NNNN_name.(up|down).sql -- rename it or move it out of {migrations_dir}"
            )
        version, name, direction = match["version"], match["name"], match["direction"]
        if direction == "up":
            if version in ups:
                raise MigrationError(
                    f"duplicate migration version {version!r}: {ups[version][1].name} and {path.name}"
                )
            ups[version] = (name, path)
        else:
            downs[version] = path

    missing_down = sorted(set(ups) - set(downs))
    if missing_down:
        raise MigrationError(
            "migration(s) with no matching .down.sql (every migration must be revertible): "
            + ", ".join(missing_down)
        )

    return [
        Migration(version=version, name=ups[version][0], up_path=ups[version][1], down_path=downs[version])
        for version in sorted(ups)
    ]


def pending(migrations: Sequence[Migration], applied_versions: set[str]) -> list[Migration]:
    """Migrations not yet applied, in version order -- the order `up` applies them
    in. Kept as pure set-and-sort logic, no subprocess, no filesystem: the
    interesting bug in this function is an off-by-one or an out-of-order
    application, not a SQL error, so it's tested without a database at all.
    """
    return [m for m in migrations if m.version not in applied_versions]


def to_revert(migrations: Sequence[Migration], applied_versions: set[str], *, steps: int) -> list[Migration]:
    """The last `steps` applied migrations, in REVERSE version order -- the order
    `down` undoes them in. Reverse matters: migration 0003 may reference a table
    0002 created, so 0003's down-script must run before 0002's can.
    """
    applied = [m for m in migrations if m.version in applied_versions]
    applied.sort(key=lambda m: m.version, reverse=True)
    return applied[:steps]


def _run_psql(
    sql: str,
    *,
    env: Mapping[str, str],
    variables: Mapping[str, str] | None = None,
    fetch: bool = False,
) -> str:
    """Feed `sql` to `psql` over stdin inside one transaction (`--single-transaction`):
    the whole script commits or none of it does, so a migration is never left half
    applied. `variables` become `psql -v name=value` substitutions, referenced from
    `sql` as `:'name'` -- psql's own safe-quoting form (it escapes embedded quotes
    itself), used instead of Python-side string interpolation into SQL so a
    migration version string never needs to be "trusted" to be injection-safe by
    construction alone.
    """
    args = ["psql", "--single-transaction", "-v", "ON_ERROR_STOP=1"]
    if fetch:
        args += ["-t", "-A"]
    for key, value in (variables or {}).items():
        args += ["-v", f"{key}={value}"]
    args += ["-f", "-"]

    result = subprocess.run(args, input=sql, capture_output=True, text=True, env=dict(env))
    if result.returncode != 0:
        raise MigrationError(f"psql failed:\n{result.stderr.strip()}")
    return result.stdout


def ensure_bootstrap(*, env: Mapping[str, str]) -> None:
    """Create `schema_migrations` if it doesn't exist yet. Idempotent, and not
    itself tracked as a migration -- every other migration depends on it existing,
    so it can't be one of the things `pending()` might decide not to apply yet.
    """
    _run_psql(_BOOTSTRAP_SQL, env=env)


def applied_versions(*, env: Mapping[str, str]) -> set[str]:
    """Every version currently recorded in `schema_migrations`. Call
    `ensure_bootstrap` first; a database with no such table yet raises rather than
    silently reporting "nothing applied", since that could just as easily mean the
    connection is pointed at the wrong database entirely.
    """
    output = _run_psql("SELECT version FROM schema_migrations ORDER BY version;", env=env, fetch=True)
    return {line for line in output.splitlines() if line}


def apply_migration(migration: Migration, *, env: Mapping[str, str]) -> None:
    """Run `migration`'s up.sql and record it as applied, in one transaction --
    never one without the other, so a crash between them can't leave a migration
    applied-but-unrecorded (silently re-applied and failing next run) or
    recorded-but-not-really-applied.
    """
    sql = migration.up_path.read_text(encoding="utf-8")
    sql += "\nINSERT INTO schema_migrations (version) VALUES (:'version');\n"
    _run_psql(sql, env=env, variables={"version": migration.version})


def revert_migration(migration: Migration, *, env: Mapping[str, str]) -> None:
    """Run `migration`'s down.sql and remove its record, in one transaction, for the
    same reason `apply_migration` does both together.
    """
    sql = migration.down_path.read_text(encoding="utf-8")
    sql += "\nDELETE FROM schema_migrations WHERE version = :'version';\n"
    _run_psql(sql, env=env, variables={"version": migration.version})
