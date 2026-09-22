#!/usr/bin/env python3
"""One-time, idempotent setup for the persistent `citadel_demo` database
`scripts/run-api.sh` runs the API against: create the database if it does
not exist yet, then apply every migration this checkpoint actually needs.

Distinct from `packages/platform/tests/pg_scratch.py`'s `pg_scratch_db()`,
which creates and drops a throwaway `citadel_test_<hex>` database per test
-- `citadel_demo` is meant to keep its seeded identities and its audit log
across restarts, which is the whole point of a checkpoint someone can come
back to.

Not simply `python -m citadel_platform.migrations up`, even though that is
the real, complete command and the one this script effectively is a
fallback for: this sandbox has no pgvector extension, so migration 0004
(pgvector document chunks) fails here the same known way
`packages/platform/AGENTS.md` and root `AGENTS.md`'s "Current state"
already document -- and `up` applies every pending migration strictly in
order with no way to skip just one. On the real WSL2 machine (ADR-0005),
where pgvector is actually installed, applying 0004 succeeds and this
script's skip branch below never runs -- this is the same "try the real
thing, fall back only on the one documented sandbox gap, log which one
happened" shape `scripts/lib/env.sh` and `scripts/dev-db.sh` already use,
applied here instead of hand-run one more time.

Invoked by `scripts/run-api.sh`, never meant to be run directly by a
person -- but nothing about it depends on that; it only needs `PG*` in its
environment, the same convention every other Postgres-talking piece of
this repo uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from citadel_platform.migrations.runner import (
    MigrationError,
    apply_migration,
    applied_versions,
    discover_migrations,
    ensure_bootstrap,
    pending,
)

#: The one migration this sandbox cannot apply, and the substring its
#: failure message is expected to contain -- both must match before a
#: MigrationError is treated as "the known gap" rather than a real problem.
_KNOWN_GAP_VERSION = "0004"
_KNOWN_GAP_SUBSTRING = "vector"

#: Relative to the repo root -- correct because scripts/lib/env.sh's
#: citadel::run always executes its command with the repo root as cwd,
#: exactly like citadel_platform.migrations.cli's own default does relative
#: to that module's __file__.
_MIGRATIONS_DIR = Path("packages") / "platform" / "migrations"


def _create_database_if_missing(target_db: str, env: dict[str, str]) -> None:
    maintenance_env = dict(env)
    maintenance_env["PGDATABASE"] = "postgres"  # always exists; used only to check/create
    check = subprocess.run(
        ["psql", "-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{target_db}'"],
        env=maintenance_env,
        capture_output=True,
        text=True,
    )
    if check.stdout.strip() == "1":
        return
    print(f"[bootstrap] creating database {target_db!r} ...")
    subprocess.run(["createdb", target_db], env=maintenance_env, check=True)


def main() -> int:
    env = dict(os.environ)
    target_db = env.get("PGDATABASE")
    if not target_db:
        print("[bootstrap] PGDATABASE is not set", file=sys.stderr)
        return 1

    _create_database_if_missing(target_db, env)

    migrations = discover_migrations(_MIGRATIONS_DIR)
    ensure_bootstrap(env=env)
    to_apply = pending(migrations, applied_versions(env=env))

    if not to_apply:
        print("[bootstrap] migrations already up to date")
        return 0

    for migration in to_apply:
        try:
            print(f"[bootstrap] applying {migration.version}_{migration.name} ...")
            apply_migration(migration, env=env)
        except MigrationError as exc:
            if migration.version == _KNOWN_GAP_VERSION and _KNOWN_GAP_SUBSTRING in str(exc).lower():
                print(
                    f"[bootstrap] SKIPPING {migration.version}_{migration.name}: pgvector is "
                    f"not installed here (documented sandbox gap -- root AGENTS.md 'Current "
                    f"state'). Deliberate and visible, not a silent failure -- revisit on the "
                    f"real WSL2 machine, which has the extension."
                )
                continue
            raise
    print("[bootstrap] migrations: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
