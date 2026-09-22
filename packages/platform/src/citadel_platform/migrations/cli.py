"""`python -m citadel_platform.migrations` -- apply, revert, or report on
packages/platform/migrations/*.sql against whatever database the standard `PG*`
environment variables point at.

Kept thin on purpose: every decision that matters (discovery, ordering, what "up"
and "down" mean) lives in citadel_platform.migrations.runner, tested there without a
subprocess or a real database. This module is just an argparse front end and the
list-formatting for `status`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from citadel_platform.migrations.runner import (
    MigrationError,
    apply_migration,
    applied_versions,
    discover_migrations,
    ensure_bootstrap,
    pending,
    revert_migration,
    to_revert,
)

# packages/platform/src/citadel_platform/migrations/cli.py -> parents[3] == packages/platform
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m citadel_platform.migrations")
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=_DEFAULT_MIGRATIONS_DIR,
        help=f"directory of NNNN_name.(up|down).sql files (default: {_DEFAULT_MIGRATIONS_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("up", help="apply every pending migration, in order")

    down_parser = subparsers.add_parser("down", help="revert the most recently applied migration(s)")
    down_parser.add_argument("--steps", type=int, default=1, help="how many migrations to revert (default: 1)")

    subparsers.add_parser("status", help="list which migrations are applied and which are pending")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = os.environ

    try:
        migrations = discover_migrations(args.migrations_dir)
        ensure_bootstrap(env=env)
        applied = applied_versions(env=env)

        if args.command == "status":
            for migration in migrations:
                mark = "applied" if migration.version in applied else "pending"
                print(f"{migration.version}  {migration.name:<40} {mark}")
            return 0

        if args.command == "up":
            to_apply = pending(migrations, applied)
            if not to_apply:
                print("nothing to do -- already at the latest migration")
                return 0
            for migration in to_apply:
                print(f"applying {migration.version}_{migration.name} ...")
                apply_migration(migration, env=env)
            print(f"applied {len(to_apply)} migration(s)")
            return 0

        if args.command == "down":
            to_undo = to_revert(migrations, applied, steps=args.steps)
            if not to_undo:
                print("nothing to do -- no applied migrations to revert")
                return 0
            for migration in to_undo:
                print(f"reverting {migration.version}_{migration.name} ...")
                revert_migration(migration, env=env)
            print(f"reverted {len(to_undo)} migration(s)")
            return 0

        raise AssertionError(f"unreachable: unknown command {args.command!r}")

    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
