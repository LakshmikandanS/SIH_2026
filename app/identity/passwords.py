"""Password hashing for the local identity table (design doc §6.4).

    "A single local table of users (bcrypt-hashed passwords) is the identity
     provider for the MVP -- explicitly documented as a development stand-in,
     not the LDAP/AD integration the full architecture names."

DEVELOPMENT STAND-IN. This module is deliberately the whole of Citadel's
credential handling for this slice, and it makes no production claim:

  * there is no password policy, no lockout, no rate limiting, no rotation,
    no MFA, and no password history;
  * there is no enrolment flow -- users are seeded by `app.identity.service`;
  * a real deployment replaces the *whole* of `app.identity.service.authenticate`
    with an LDAP/AD bind. Nothing outside this package looks at a password
    hash, so that replacement is contained (see the Phase-2 seam in
    docs/BUILD_LOG.md).

bcrypt itself is the one production-grade part: the cost factor is bcrypt's
default and verification is constant-time via `bcrypt.checkpw`.
"""

from __future__ import annotations

import bcrypt

#: bcrypt hashes at most the first 72 bytes of a password and silently ignores
#: the rest, which would make two different long passwords interchangeable.
#: Reject rather than truncate -- fail closed (AGENTS.md §9).
MAX_PASSWORD_BYTES = 72


class PasswordTooLong(ValueError):
    def __init__(self, length: int) -> None:
        super().__init__(
            f"password is {length} bytes; bcrypt hashes only the first "
            f"{MAX_PASSWORD_BYTES}, so longer passwords are rejected rather "
            f"than silently truncated"
        )


def hash_password(password: str) -> str:
    """Return a bcrypt hash suitable for `User.password_hash`."""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(len(encoded))
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time check of `password` against a stored hash.

    A user row with no hash (the column is nullable -- see the foundation
    schema) can never authenticate: absent credentials are not blank
    credentials.
    """
    if not password_hash:
        return False
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except ValueError:
        # Malformed / non-bcrypt hash in the column: not a match, not a crash.
        return False
