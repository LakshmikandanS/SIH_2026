"""Discovers, applies and reverts packages/platform/migrations/*.sql.

See citadel_platform.migrations.runner for the design rationale (psql-based, no
psycopg dependency) and citadel_platform.migrations.cli for the `python -m
citadel_platform.migrations` command line.
"""

from __future__ import annotations

from citadel_platform.migrations.runner import (
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

__all__ = [
    "Migration",
    "MigrationError",
    "apply_migration",
    "applied_versions",
    "discover_migrations",
    "ensure_bootstrap",
    "pending",
    "revert_migration",
    "to_revert",
]
