"""Capability issuance and verification (design doc §6.5, §3).

    "Format: a signed token (HMAC-SHA256, single trusted issuer -- the Control
     Plane process itself, since it and the verifier share a process in this
     MVP's trust zone per §2), short TTL (5 minutes), scoped to exactly one
     operation and one task/agent pair. [...] Issued once per plan step,
     immediately before that step executes (not all at once at task start --
     this keeps the blast radius of a leaked token to one operation).
     **No revocation list exists for the MVP** -- the 5-minute TTL is the only
     expiry mechanism."

Wire format, reconciling two places the doc describes the same object:
§1.1 step 8 calls it "the capability JWT"; §3 shows the object with its fields
plus a trailing `"signature"`. A JWS with HS256 *is* an HMAC-SHA256 signature
over those fields, so one artefact satisfies both readings -- the compact JWT's
third segment is §3's `signature`. Every §6.5 field is carried as a claim under
its own name, so a decoded token reads exactly like the doc's example.

`expires_at` (§6.5, ISO 8601) and `exp` (the registered JWT claim the library
enforces) are both present and are cross-checked on every verification: if
they ever disagree, the token is rejected rather than the more permissive of
the two being believed.

What this module deliberately does NOT have:
  * a revocation list, or any store of issued capabilities -- BB-020 resolves
    to short TTL only, and §6.8's `DISABLE TOOL` covers the case revocation
    would have been needed for, at the policy layer, on every call;
  * any notion of "renew" or "extend" -- a step that needs longer gets a new
    token for that step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import jwt

from app import config, ids
from app.db.base import utcnow

#: Marks the token as a capability, so a session JWT (§6.4) can never be
#: presented where a capability is expected even if the two keys were ever
#: misconfigured to be the same.
TOKEN_TYPE = "capability"


class CapabilityInvalid(Exception):
    """Signature, structure, type, or operation/task/agent binding is wrong.

    Maps to the §6.6 error code `CAPABILITY_INVALID`.
    """


class CapabilityExpired(Exception):
    """The token is past its `expires_at`. Maps to `CAPABILITY_EXPIRED`.

    Kept distinct from `CapabilityInvalid` for one specific reason: §9's
    checklist has "Expired capability -> DENY" as its own line, and §6.6 gives
    expiry its own error code, so the demo must be able to show that a token
    that was genuinely issued to this agent has simply aged out.

    `.capability` carries the claims when they are recoverable. That is safe
    and it matters: the *signature* verified, only the clock failed, so
    `task_id`/`agent_id` are as trustworthy as they ever were and the resulting
    `TOOL_DENIED` event can be attributed to the right task instead of landing
    unattributed in the chain. A bad *signature* recovers nothing, by design.
    """

    def __init__(self, message: str, capability: Optional["Capability"] = None) -> None:
        super().__init__(message)
        self.capability = capability


@dataclass(frozen=True)
class CapabilityScope:
    """§6.5's `scope`: `{"classification_max": ..., "department": ...}`.

    Not read by the Tool Gateway's Step A -- §6.6 defines that check as
    "signature, expiry, operation match". The scope travels onward as the
    `requester` block of §6.9's Data Plane contract, where filtering actually
    happens inside the Data Plane.
    """

    classification_max: str
    department: str

    def to_claim(self) -> dict[str, str]:
        return {
            "classification_max": self.classification_max,
            "department": self.department,
        }

    @classmethod
    def from_claim(cls, claim: Mapping[str, Any]) -> "CapabilityScope":
        if not isinstance(claim, Mapping):
            raise CapabilityInvalid("capability scope is not an object")
        try:
            return cls(
                classification_max=str(claim["classification_max"]),
                department=str(claim["department"]),
            )
        except KeyError as exc:
            raise CapabilityInvalid(f"capability scope is missing {exc}") from None


@dataclass(frozen=True)
class Capability:
    """§3's Capability object, as verified. Only `verify_capability` produces
    one, so holding an instance means a signature was checked."""

    capability_id: str
    task_id: str
    agent_id: str
    operation: str
    scope: CapabilityScope
    expires_at: datetime
    issued_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        """The §6.5 shape, for event payloads and the Data Plane `requester`."""
        return {
            "capability_id": self.capability_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "operation": self.operation,
            "scope": self.scope.to_claim(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class IssuedCapability:
    """A freshly minted capability: the signed token to hand to the caller,
    and the claims it carries, so the issuer need not decode its own token."""

    token: str
    capability: Capability

    @property
    def capability_id(self) -> str:
        return self.capability.capability_id

    @property
    def expires_at(self) -> datetime:
        return self.capability.expires_at


def issue_capability(
    *,
    task_id: str,
    agent_id: str,
    operation: str,
    scope: CapabilityScope,
    ttl_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> IssuedCapability:
    """Mint one capability for one operation on one task/agent pair.

    `ttl_seconds` defaults to §6.5's 300. It is a parameter for exactly two
    reasons: the suite has to be able to produce an already-expired token
    without waiting five minutes, and a Phase-2 deployment may want the TTL
    configurable. It is not an invitation for a caller to ask for longer --
    the default is the contract.

    Call this immediately before the step it authorizes runs, once per step
    (§6.5). Issuing all of a plan's capabilities up front would restore
    exactly the blast radius the 5-minute TTL exists to bound.

    No event is emitted here: §6.12's closed vocabulary has `CAPABILITY_CHECKED`
    and no issuance type, and the Tool Gateway's check is where the token's
    use becomes auditable.
    """
    issued = now or utcnow()
    ttl = config.CAPABILITY_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    expires_at = issued + timedelta(seconds=ttl)

    capability = Capability(
        capability_id=ids.new_id(ids.CAPABILITY),
        task_id=task_id,
        agent_id=agent_id,
        operation=operation,
        scope=scope,
        expires_at=expires_at,
        issued_at=issued,
    )

    claims: dict[str, Any] = {
        "typ": TOKEN_TYPE,
        "capability_id": capability.capability_id,
        "task_id": capability.task_id,
        "agent_id": capability.agent_id,
        "operation": capability.operation,
        "scope": capability.scope.to_claim(),
        # §6.5's field, kept in the doc's ISO form ...
        "expires_at": expires_at.isoformat(),
        # ... and the registered claim the JWT library enforces. Cross-checked
        # on verification; they are never allowed to disagree.
        "exp": int(expires_at.timestamp()),
        "iat": int(issued.timestamp()),
    }
    token = jwt.encode(claims, config.CAPABILITY_SECRET, algorithm=config.JWT_ALGORITHM)
    return IssuedCapability(token=token, capability=capability)


def verify_capability(
    token: str,
    *,
    operation: str,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Capability:
    """§6.6 Step A: signature, expiry, operation match. Local, no network call.

    Also enforces the task/agent binding when the caller states it -- §6.5
    scopes a token "to exactly one operation and one task/agent pair", so a
    token minted for another task must not be usable here.

    Raises `CapabilityExpired` or `CapabilityInvalid`. Never returns a partial
    result: fail closed.
    """
    if not token:
        raise CapabilityInvalid("no capability token presented")

    try:
        claims = jwt.decode(
            token,
            config.CAPABILITY_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise CapabilityExpired(
            "capability token has expired; §6.5 gives capabilities a 5-minute "
            "TTL and there is no renewal",
            capability=_recover_expired(token),
        ) from None
    except jwt.PyJWTError as exc:
        raise CapabilityInvalid(f"capability token rejected: {exc}") from None

    capability = _claims_to_capability(claims)

    # Operation match -- the check that makes a capability "coarse
    # pre-authorization" rather than a bearer token for everything (C-002).
    if capability.operation != operation:
        raise CapabilityInvalid(
            f"capability authorizes {capability.operation!r}, not {operation!r}"
        )
    if task_id is not None and capability.task_id != task_id:
        raise CapabilityInvalid(
            f"capability is bound to task {capability.task_id!r}, not {task_id!r}"
        )
    if agent_id is not None and capability.agent_id != agent_id:
        raise CapabilityInvalid(
            f"capability is bound to agent {capability.agent_id!r}, not {agent_id!r}"
        )

    return capability


def _claims_to_capability(claims: Mapping[str, Any]) -> Capability:
    """Structural validation of a signature-verified claim set."""
    if claims.get("typ") != TOKEN_TYPE:
        raise CapabilityInvalid("token is not a capability token")

    for field in ("capability_id", "task_id", "agent_id", "operation", "expires_at"):
        value = claims.get(field)
        if not isinstance(value, str) or not value:
            raise CapabilityInvalid(f"capability token is missing {field!r}")

    expires_at = _parse_iso(claims["expires_at"])
    if expires_at is None:
        raise CapabilityInvalid("capability expires_at is not a valid timestamp")
    if int(expires_at.timestamp()) != int(claims["exp"]):
        # Both were signed, so disagreement means the issuer is buggy rather
        # than that an attacker forged one -- either way, do not pick a winner.
        raise CapabilityInvalid("capability expires_at disagrees with exp")

    return Capability(
        capability_id=claims["capability_id"],
        task_id=claims["task_id"],
        agent_id=claims["agent_id"],
        operation=claims["operation"],
        scope=CapabilityScope.from_claim(claims.get("scope") or {}),
        expires_at=expires_at,
        issued_at=_from_epoch(claims.get("iat")),
    )


def _recover_expired(token: str) -> Optional[Capability]:
    """Re-read an expired token's claims *with the signature still verified*.

    Only `verify_exp` is relaxed -- the HMAC is checked exactly as before, so
    nothing unsigned is ever recovered. Used solely to attribute the resulting
    denial event to the task the token was genuinely issued for.
    """
    try:
        claims = jwt.decode(
            token,
            config.CAPABILITY_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            options={"verify_exp": False, "require": ["exp", "iat"]},
        )
        return _claims_to_capability(claims)
    except (jwt.PyJWTError, CapabilityInvalid):
        return None


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _from_epoch(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
