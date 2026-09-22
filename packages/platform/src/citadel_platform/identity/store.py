"""Reading `users` rows back as `citadel_contracts.domain.User`.

The read side of PLAN-M0 task 7 that nothing had needed yet: migrations
0002/0005/0006 create and seed the table, `citadel_platform.identity.keys`
owns the signing key, `citadel_contracts.identity` signs and verifies a
token once a `User` exists -- but nothing before this module actually turned
a Postgres row back into one. `psql`-subprocess, not `psycopg`, for the same
reason as `citadel_platform.audit.psql_client`: no driver is installable in
this sandbox, and a login lookup is low-frequency, read-mostly access
exactly like `citadel_platform.migrations`'s own use of `psql`.

`User.user_id` is `external_identity` (`'demo-engineer-1'`, ...), not the
row's UUID `id` -- migration 0002's own comment on that column: "the subject
claim from a verified session token", and `citadel_contracts.identity._to_
claims` puts `user.user_id` in the token's `sub`. Using the UUID instead
would mean the token's `sub` and the row it came from disagree about which
field is "the identity". `User.username` is `display_name` -- the
human-facing name ('R. Kulkarni'), never shown to anyone as a secret, unlike
`external_identity` which is closer to an internal handle.
"""

from __future__ import annotations

from typing import Mapping, Optional

from citadel_contracts.domain import User

from citadel_platform._psql import run_psql_csv

_SELECT_COLUMNS = "display_name, department, clearance, role, external_identity"


def _row_to_user(display_name: str, department: str, clearance: str, role: str, external_identity: str) -> User:
    return User(
        user_id=external_identity,
        username=display_name,
        roles=(role,),
        clearance=clearance,
        department=department,
    )


def list_users(env: Mapping[str, str]) -> list[User]:
    """Every seeded user, ordered by `external_identity` -- stable and
    deterministic, so a UI listing them (a login picker) doesn't reorder
    itself between calls for no reason.
    """
    parsed = run_psql_csv(
        f"SELECT {_SELECT_COLUMNS} FROM users ORDER BY external_identity", env=env
    )
    if not parsed:
        return []
    _header, *data_rows = parsed
    return [_row_to_user(*row) for row in data_rows]


def get_user_by_external_identity(env: Mapping[str, str], external_identity: str) -> Optional[User]:
    """The one user whose `external_identity` matches, or `None`. Used by the
    demo login endpoint: it is the caller's job to decide what "no such
    user" means (a 404, an auth failure) -- this function just reports
    absence, the same posture `citadel_platform.registry`'s lookup helpers
    take with `KeyError` for a registry-backed name, adapted here to
    `Optional` because "no such demo user" is an ordinary, expected outcome
    of a login attempt, not a programmer error.
    """
    parsed = run_psql_csv(
        f"SELECT {_SELECT_COLUMNS} FROM users WHERE external_identity = :'external_identity'",
        env=env,
        variables={"external_identity": external_identity},
    )
    if not parsed:
        return None
    _header, *data_rows = parsed
    if not data_rows:
        return None
    return _row_to_user(*data_rows[0])


__all__ = ["list_users", "get_user_by_external_identity"]
