"""Session JWT issuance and verification (design doc §6.4).

    "`POST /login` issues a signed session JWT (8h expiry) carrying
     {user_id, roles}. Every subsequent endpoint derives the acting identity
     from this verified JWT -- a client-supplied `user_id` in a request body is
     never trusted and is ignored if present."

This module is the only place a session token is minted or opened. Everything
downstream receives a `SessionIdentity` -- a value object that can only be
produced by verifying a signature -- so "identity came from the token" is a
type-level fact rather than a convention a caller could forget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app import config
from app.db.base import utcnow

#: Distinguishes a session token from a capability token (§6.5) in the signed
#: payload itself. The two already use different keys, so this is belt and
#: braces -- but it makes token confusion impossible to reintroduce by
#: accidentally sharing a secret later.
TOKEN_TYPE = "session"


class SessionInvalid(Exception):
    """The presented session token is missing, malformed, mis-signed, of the
    wrong type, or expired. One exception, deliberately: distinguishing them
    for the caller tells an attacker which part of a forgery was wrong."""


@dataclass(frozen=True)
class SessionIdentity:
    """The acting identity, derived from a verified signature and nothing else.

    There is no constructor path from a request body to this object. §6.4's
    rule -- "never trust a client-supplied user_id" -- is enforced by that
    absence, not by remembering to check.
    """

    user_id: str
    roles: tuple[str, ...] = field(default=())
    issued_at: datetime | None = None
    expires_at: datetime | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles


def issue_session_token(
    user_id: str,
    roles: list[str] | tuple[str, ...],
    *,
    ttl_hours: int | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Mint a session JWT. Returns `(token, expires_at)`.

    `ttl_hours` defaults to §6.4's 8 hours (`config.SESSION_TTL_HOURS`); it is
    a parameter only so the suite can prove expiry without waiting 8 hours.
    """
    issued = now or utcnow()
    hours = config.SESSION_TTL_HOURS if ttl_hours is None else ttl_hours
    expires = issued + timedelta(hours=hours)

    claims: dict[str, Any] = {
        "typ": TOKEN_TYPE,
        "user_id": user_id,
        "roles": list(roles),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, config.SESSION_SECRET, algorithm=config.JWT_ALGORITHM)
    return token, expires


def verify_session_token(token: str) -> SessionIdentity:
    """Verify signature, type and expiry; return the acting identity.

    Raises `SessionInvalid` on anything at all wrong -- fail closed.
    """
    if not token:
        raise SessionInvalid("no session token presented")
    try:
        claims = jwt.decode(
            token,
            config.SESSION_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise SessionInvalid(f"session token rejected: {exc}") from None

    if claims.get("typ") != TOKEN_TYPE:
        raise SessionInvalid("token is not a session token")
    user_id = claims.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise SessionInvalid("session token carries no user_id")

    roles = claims.get("roles") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise SessionInvalid("session token carries a malformed roles claim")

    return SessionIdentity(
        user_id=user_id,
        roles=tuple(roles),
        issued_at=_from_epoch(claims.get("iat")),
        expires_at=_from_epoch(claims.get("exp")),
    )


def _from_epoch(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
