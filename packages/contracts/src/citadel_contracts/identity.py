"""Verified session identity.

PLAN-M0 task 7 / ADR-0001 §Q5 ("Identity, roles, per-user ACL filtering" moved to
M0 because §Q7's demonstration needs it) and §Q7 (the three-identity cast: two
engineers at different classification levels in different departments, and one
approver). Root AGENTS.md invariant 3: "Identity comes from a verified session
token, never from a request body." `tests/structural/test_identity_not_from_body.py`
is the structural side of that invariant -- it fails the build the moment a
handler reads `body["user_id"]` instead of going through this module's output.
This module is the other side: the thing a handler is supposed to read instead.

Shaped after `receipts.py`'s DecisionReceipt, deliberately -- same package, same
signing story (Ed25519 via PyJWT), same "distinct `typ`, distinct key per token
kind" discipline (`TOKEN_TYPE = "session"`, never `"receipt"`, and signed with its
own keypair -- a session-signing key compromise must never let an attacker forge
receipts, or vice versa). It is simpler than a receipt on purpose, in two ways
that both follow from what a session actually is:

  * No nonce / single-use tracking. A receipt authorizes one call, once; a
    session is presented on every request for as long as it is valid. Treating
    it as single-use would make normal use indistinguishable from a replay.
  * No parallel ISO-8601 timestamp fields cross-checked against `iat`/`exp`.
    DecisionReceipt keeps both because `to_claims`/`from_claims` round-trip a
    receipt as a value object with human-readable fields callers inspect
    directly. A session token is presented once, verified, and turned straight
    into the `User` a handler needs; nothing downstream re-reads its raw
    timestamps, so there is nothing for a second representation to protect
    against drifting from.

Output type is `citadel_contracts.domain.User` itself -- not a parallel
"Identity"/"Principal" type -- because `User` already carries exactly the fields
a verified session needs to assert (`user_id`, `username`, `roles`, `clearance`,
`department`) and every downstream consumer (the policy evaluator in
`citadel_tools`, an audit event's `actor_id`) wants a `User`, not a second shape
that means the same thing. `receipts.py`'s `ResourceLike` Protocol duck-types its
*input* so a future caller need not construct a `Resource`; there is no
equivalent need here, since `User` is already the canonical shape everything else
expects on the way *out*.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from citadel_contracts.domain import User

#: Marks the token as a session, so it can never be presented where a receipt
#: or capability token is expected -- receipts.py's own `TOKEN_TYPE` comment
#: names exactly this reasoning; the two constants deliberately do not share a
#: definition, so that changing one can never silently change the other.
TOKEN_TYPE = "session"

#: Ed25519, matching receipts.py's choice for the same reason: one signature
#: algorithm for every token in this system. A distinct constant from
#: receipts.py's `SIGNING_ALGORITHM` (same value, "EdDSA") rather than an
#: import of it -- these are independent token kinds and independent keys;
#: sharing the constant would be a coupling this module does not need.
SIGNING_ALGORITHM = "EdDSA"

#: An 8-hour workday session. A default, not a ceiling -- every caller of
#: `issue_session_token` can override `ttl_seconds`; nothing here hardcodes a
#: policy decision about how long a session should live.
DEFAULT_TTL_SECONDS: float = 8 * 60 * 60


class SessionTokenInvalid(Exception):
    """Signature, structure, or token kind is wrong.

    One exception for all of those, mirroring `ReceiptInvalid`'s own reasoning:
    a caller needs to know "refuse and ask the user to sign in again", and
    telling an attacker which specific check a forged token failed is not a
    service to anyone.
    """


class SessionTokenExpired(Exception):
    """The session is past its `exp`.

    Kept distinct from `SessionTokenInvalid`, exactly as `ReceiptExpired` is
    kept distinct from `ReceiptInvalid` -- "the session was valid and simply
    outlived its window" is a different, separately provable line from "this
    was never a valid session at all". `.user` carries the identity when the
    signature verified and only the clock failed, so a caller can still log
    *who* it was that needs to sign in again.
    """

    def __init__(self, message: str, user: Optional[User] = None) -> None:
        super().__init__(message)
        self.user = user


def _to_claims(user: User, *, issued_at: datetime, expires_at: datetime) -> dict[str, Any]:
    return {
        "typ": TOKEN_TYPE,
        "sub": user.user_id,
        "username": user.username,
        "roles": list(user.roles),
        "clearance": user.clearance,
        "department": user.department,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def _user_from_claims(claims: Mapping[str, Any]) -> User:
    """Structural validation of a signature-verified claim set. Raises
    `SessionTokenInvalid` on anything wrong with the shape -- fail closed,
    the same posture `DecisionReceipt.from_claims` takes."""
    if claims.get("typ") != TOKEN_TYPE:
        raise SessionTokenInvalid("token is not a session token")

    user_id = claims.get("sub")
    username = claims.get("username")
    if not isinstance(user_id, str) or not user_id:
        raise SessionTokenInvalid("session token is missing 'sub'")
    if not isinstance(username, str) or not username:
        raise SessionTokenInvalid("session token is missing 'username'")

    roles = claims.get("roles")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise SessionTokenInvalid("session token 'roles' must be a list of strings")

    clearance = claims.get("clearance")
    department = claims.get("department")
    if not isinstance(clearance, str):
        raise SessionTokenInvalid("session token 'clearance' must be a string")
    if not isinstance(department, str):
        raise SessionTokenInvalid("session token 'department' must be a string")

    return User(
        user_id=user_id,
        username=username,
        roles=tuple(roles),
        clearance=clearance,
        department=department,
    )


def issue_session_token(
    user: User,
    *,
    private_key: Ed25519PrivateKey,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: Optional[datetime] = None,
) -> str:
    """Sign `user` into a bearer session token. A pure function -- this module
    does not restrict who may call it with a key they hold, exactly like
    `sign_receipt`; `citadel_platform`'s login path (once written) is the
    *intended* sole caller of this with the real signing key, not something
    this function enforces itself.
    """
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    claims = _to_claims(user, issued_at=issued_at, expires_at=expires_at)
    # PyJWT's stubs type encode()'s return as Any; it is always a str (v2's
    # default). The wrap is for mypy --strict, not a runtime behaviour change --
    # see sign_receipt's identical comment.
    return str(jwt.encode(claims, private_key, algorithm=SIGNING_ALGORITHM))


def verify_session_token(
    token: str,
    *,
    public_key: Ed25519PublicKey,
    now: Optional[datetime] = None,
) -> User:
    """The verifying path: signature, then token kind, then expiry, in that
    order. Raises on any failure -- fail closed. This is what a handler calls
    instead of reading identity from its request body (root AGENTS.md
    invariant 3); the discard of any caller-supplied identity field is this
    function's entire reason to exist.
    """
    if not token:
        raise SessionTokenInvalid("no session token presented")

    # Signature is fully enforced by jwt.decode below. Expiry is deliberately
    # NOT delegated to PyJWT's own wall-clock `exp` check (`verify_exp` is
    # off): this function checks expiry itself, against `now`, which is the
    # only way it can ever be asked "is this expired AS OF the moment I am
    # checking" -- real time by default, an injected time in a test. A bad
    # signature still fails here regardless, since only `verify_exp` is
    # relaxed. Same posture as verify_receipt.
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[SIGNING_ALGORITHM],
            options={"verify_exp": False, "require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise SessionTokenInvalid(f"session token rejected: {exc}") from None

    user = _user_from_claims(claims)

    reference_now = now or datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    if expires_at <= reference_now:
        raise SessionTokenExpired(
            "session token has expired; sign in again", user=user
        )

    return user


__all__ = [
    "TOKEN_TYPE",
    "SIGNING_ALGORITHM",
    "DEFAULT_TTL_SECONDS",
    "SessionTokenInvalid",
    "SessionTokenExpired",
    "issue_session_token",
    "verify_session_token",
]
