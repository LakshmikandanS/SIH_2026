"""citadel_platform.identity.store -- reading `users` rows back as
citadel_contracts.domain.User, proven against a real Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_platform.identity.store import get_user_by_external_identity, list_users
from citadel_platform.migrations import apply_migration, discover_migrations, ensure_bootstrap
from pg_scratch import pg_scratch_db

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"

pytestmark = pytest.mark.integration


def _apply(env: dict[str, str], version: str) -> None:
    ensure_bootstrap(env=env)
    migration = next(m for m in discover_migrations(REAL_MIGRATIONS_DIR) if m.version == version)
    apply_migration(migration, env=env)


def test_the_real_seeded_cast_round_trips_correctly():
    with pg_scratch_db() as env:
        for version in ("0002", "0005", "0006"):
            _apply(env, version)

        users = list_users(env)
        assert [u.user_id for u in users] == ["demo-approver", "demo-engineer-1", "demo-engineer-2"]

        engineer_1 = next(u for u in users if u.user_id == "demo-engineer-1")
        assert engineer_1.username == "R. Kulkarni"
        assert engineer_1.roles == ("engineer",)
        assert engineer_1.clearance == "internal"
        assert engineer_1.department == "process-engineering"

        engineer_2 = next(u for u in users if u.user_id == "demo-engineer-2")
        assert engineer_2.clearance == "confidential"
        assert engineer_2.department == "instrumentation"
        assert engineer_2.department != engineer_1.department  # ADR-0001 §Q7: different departments
        assert engineer_2.clearance != engineer_1.clearance  # ...and different clearances

        approver = next(u for u in users if u.user_id == "demo-approver")
        assert approver.roles == ("approver",)


def test_get_by_external_identity_finds_the_right_row():
    with pg_scratch_db() as env:
        for version in ("0002", "0005", "0006"):
            _apply(env, version)

        user = get_user_by_external_identity(env, "demo-approver")
        assert user is not None
        assert user.username == "V. Rangan"
        assert user.roles == ("approver",)


def test_get_by_external_identity_returns_none_for_no_such_user():
    with pg_scratch_db() as env:
        for version in ("0002", "0005", "0006"):
            _apply(env, version)

        assert get_user_by_external_identity(env, "nobody-seeded-this") is None


def test_list_users_on_an_empty_table_returns_an_empty_list():
    with pg_scratch_db() as env:
        for version in ("0002", "0005"):
            _apply(env, version)

        assert list_users(env) == []
