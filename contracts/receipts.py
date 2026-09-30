"""The Decision Receipt (Citadel Target Architecture §2; P1 §1-§4).

The mechanism that makes the target's invariant real: "every privileged
operation is decided by the Control Plane, proven by a signed receipt that
the executing service verifies for itself, and recorded in a chain no
participant can rewrite." Capability and receipt stay two checks, exactly as
C-002 resolved -- the receipt makes the second one (the resource-dependent
question) portable across a process boundary rather than collapsing it into
the first.

    // Decision Receipt -- signed Ed25519 by the Control Plane, which alone
    // holds the private half of the `receipt` keypair (P1 §2)
    {
      "decision_id":     "DEC-01J8...",
      "task_id":         "T123",
      "agent_id":        "A123",
      "operation":       "rag.search",
      "resource_digest": "sha256:9c1b...",
      "scope":           {"classification_max": "CONFIDENTIAL", "department": "maintenance"},
      "rule":            "tool_in_allow_list",
      "issued_at":       "2026-09-21T10:00:00Z",
      "expires_at":      "2026-09-21T10:00:30Z",
      "nonce":           "5f3a..."
    }

**A receipt is only ever issued for an ALLOW.** A denial returns an error
envelope and no token -- there is nothing to replay, and P1 §3's
`decide_and_issue` is the only thing in the system that calls `sign_receipt`,
and only on ALLOW. This module does not enforce that by itself (it is a pure
signing/verifying primitive, callable by anyone who holds the private key);
`tests/invariants/` is what proves only one call site ever does.

This module carries its own minimal id/nonce generation (`new_decision_id`,
`new_nonce`) rather than importing `app.ids`, for the one reason that matters
in this step: `contracts/` imports nothing from the rest of the repository,
ever, and `app.ids` is part of the repository. The format mirrors
`app.ids.new_id` deliberately (same six-hex-character shape, new "DEC"
prefix) so a decision id reads like every other id in the system; whether
`app.ids` itself folds into `contracts/` in a later phase is not this step's
question to answer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Marks the token as a receipt, so it can never be presented where a session
#: or capability token is expected -- the same belt-and-braces reasoning
#: `app/capability/tokens.py::TOKEN_TYPE` already applies, extended to a
#: third token kind now that all three sign with distinct keys (P1 §2).
TOKEN_TYPE = "receipt"

#: Ed25519 from P1 onward, for every token in the system (P1 §2) -- this is
#: the one signing algorithm `contracts/` speaks, full stop.
SIGNING_ALGORITHM = "EdDSA"

_DECISION_PREFIX = "DEC"


def new_decision_id() -> str:
    """A collision-free decision id in the `app.ids` shape (`DEC` + 6 hex)."""
    return f"{_DECISION_PREFIX}{uuid.uuid4().hex[:6].upper()}"


def new_nonce() -> str:
    """A single-use, unpredictable nonce. `secrets`, not `uuid`, because a
    nonce's job is to be unguessable, not merely unique."""
    return secrets.token_hex(16)


@runtime_checkable
class ResourceLike(Protocol):
    """Structural type for `resource_digest`'s argument -- anything shaped
    like `contracts.domain.Resource`. Duck-typed rather than importing
    `Resource` by name so a future caller can pass its own equivalent
    without this module caring, as long as the four fields agree."""

    resource_id: str
    type: str
    classification: str
    acl: Sequence[str]


def resource_digest(resource: "ResourceLike") -> str:
    """The canonical resource digest -- P1 §1.1, the only implementation.

    Sorted keys, no whitespace, sorted ACL, UTF-8. Every issuer and every
    verifier calls exactly this function; a digest computed two ways is a
    digest that fails in production and passes in tests.

    The digest binds *agreement between what the Control Plane approved and
    what the executing service is about to do*. For `rag.search` the
    descriptor is scope-shaped (classification ceiling and department) and
    the Data Plane filters against it; for a single-document read it names
    that document. Either way, if the descriptor presented to the executing
    service differs by one character from the one the Control Plane
    approved, the digest does not recompute and the call is refused.
    """
    payload = {
        "resource_id": resource.resource_id,
        "type": resource.type,
        "classification": resource.classification,
        "acl": sorted(resource.acl),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ReceiptInvalid(Exception):
    """Signature, structure, type, operation, digest, or nonce is wrong.

    Deliberately one exception for all of those, the same way
    `CapabilityInvalid` covers everything but expiry for a capability: a
    verifier's caller needs to know "refuse", and telling an attacker which
    specific check a forgery failed is not a service to anyone.
    """


class ReceiptExpired(Exception):
    """The receipt is past its `expires_at`.

    Kept distinct from `ReceiptInvalid` for the same reason
    `CapabilityExpired` is kept distinct from `CapabilityInvalid`: P1 §8's
    adversarial checklist has "hold a receipt for forty seconds, then use
    it -- it must refuse on expiry" as its own, separately provable line.

    `.receipt` carries the claims when they are recoverable -- the signature
    verified, only the clock failed, so the fields are as trustworthy as
    they ever were and a resulting denial can still be attributed to the
    right task. A bad *signature* recovers nothing, by design.
    """

    def __init__(self, message: str, receipt: Optional["DecisionReceipt"] = None) -> None:
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True)
class DecisionReceipt:
    """A signed proof that the Control Plane decided ALLOW for exactly this
    call, on exactly this resource, once. Only ever issued for an ALLOW."""

    decision_id: str
    task_id: str
    agent_id: str
    operation: str
    resource_digest: str
    scope: Mapping[str, str]
    rule: str
    issued_at: datetime
    expires_at: datetime
    nonce: str

    def to_claims(self) -> dict[str, Any]:
        return {
            "typ": TOKEN_TYPE,
            "decision_id": self.decision_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "operation": self.operation,
            "resource_digest": self.resource_digest,
            "scope": dict(self.scope),
            "rule": self.rule,
            # §2's ISO fields ...
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            # ... and the registered JWT claims the library enforces.
            # Cross-checked on verification, exactly as capability tokens
            # cross-check `expires_at` against `exp` -- they are never
            # allowed to disagree.
            "iat": int(self.issued_at.timestamp()),
            "exp": int(self.expires_at.timestamp()),
            "nonce": self.nonce,
        }

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> "DecisionReceipt":
        """Structural validation of a signature-verified claim set. Raises
        `ReceiptInvalid` on anything wrong with the shape -- fail closed."""
        if claims.get("typ") != TOKEN_TYPE:
            raise ReceiptInvalid("token is not a receipt token")

        for field_name in (
            "decision_id",
            "task_id",
            "agent_id",
            "operation",
            "resource_digest",
            "rule",
            "nonce",
        ):
            value = claims.get(field_name)
            if not isinstance(value, str) or not value:
                raise ReceiptInvalid(f"receipt is missing {field_name!r}")

        scope = claims.get("scope")
        if not isinstance(scope, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in scope.items()
        ):
            raise ReceiptInvalid("receipt scope must be an object of string to string")

        issued_at = _parse_iso(claims.get("issued_at"))
        expires_at = _parse_iso(claims.get("expires_at"))
        if issued_at is None:
            raise ReceiptInvalid("receipt issued_at is not a valid timestamp")
        if expires_at is None:
            raise ReceiptInvalid("receipt expires_at is not a valid timestamp")

        # Both representations were signed together; disagreement means the
        # issuer is buggy rather than that an attacker forged one -- either
        # way, do not pick a winner (mirrors capability tokens' expires_at
        # vs exp cross-check, applied here to both timestamps this schema
        # names explicitly).
        if "iat" not in claims or int(issued_at.timestamp()) != int(claims["iat"]):
            raise ReceiptInvalid("receipt issued_at disagrees with iat")
        if "exp" not in claims or int(expires_at.timestamp()) != int(claims["exp"]):
            raise ReceiptInvalid("receipt expires_at disagrees with exp")

        return cls(
            decision_id=claims["decision_id"],
            task_id=claims["task_id"],
            agent_id=claims["agent_id"],
            operation=claims["operation"],
            resource_digest=claims["resource_digest"],
            scope=dict(scope),
            rule=claims["rule"],
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=claims["nonce"],
        )


def sign_receipt(receipt: DecisionReceipt, private_key: Ed25519PrivateKey) -> str:
    """Turn a `DecisionReceipt` into a bearer token. A pure function: this
    module does not restrict who may call it with a key they hold -- P1 §3's
    `decide_and_issue` is the sole *intended* call site, and
    `tests/invariants/` is what proves it is the only actual one."""
    return jwt.encode(receipt.to_claims(), private_key, algorithm=SIGNING_ALGORITHM)


@runtime_checkable
class NonceStore(Protocol):
    """Per-verifier single-use tracking. P1 §4: "each verifier keeps its own
    nonce set -- a shared one would be a shared trust assumption, which is
    the thing being removed." `verify_receipt` takes one as a parameter
    rather than reaching for a module global for exactly that reason."""

    def seen(self, nonce: str) -> bool: ...

    def record(self, nonce: str, ttl_seconds: float) -> None: ...


class InMemoryNonceStore:
    """A `NonceStore` with TTL-based expiry, kept in memory.

    Not persisted, deliberately: a receipt's TTL is seconds (P1 §4: "single
    machine, so no skew allowance"), so a process restart's nonce-amnesia is
    bounded by the same window the receipt was already about to expire in.
    Threaded with a lock, the same discipline `ToolDisabledRegistry`
    (`app/policy/tool_disabled.py`) already applies to its own small piece
    of shared, mutable, security-relevant state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expires_at: dict[str, float] = {}  # nonce -> monotonic deadline

    def seen(self, nonce: str) -> bool:
        with self._lock:
            self._purge_locked()
            return nonce in self._expires_at

    def record(self, nonce: str, ttl_seconds: float) -> None:
        with self._lock:
            self._purge_locked()
            self._expires_at[nonce] = time.monotonic() + max(0.0, ttl_seconds)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [n for n, deadline in self._expires_at.items() if deadline <= now]
        for n in expired:
            del self._expires_at[n]

    def clear(self) -> None:
        """Test-suite reset only."""
        with self._lock:
            self._expires_at.clear()


def verify_receipt(
    token: str,
    *,
    public_key: Ed25519PublicKey,
    operation: str,
    resource: "ResourceLike",
    seen_nonces: NonceStore,
    now: Optional[datetime] = None,
) -> DecisionReceipt:
    """P1 §4's verifying path, in §4's order. Raises on any failure -- fail
    closed. Called by the executing service itself, immediately before it
    acts, never at an edge it shares with its caller.

      1. signature against the receipt public key
      2. not expired  (single machine, so no skew allowance)
      3. receipt.operation == operation
      4. receipt.resource_digest == resource_digest(resource) -- recomputed
         here, now, over the resource this verifier actually received
      5. receipt.nonce not in seen_nonces -> then record it, TTL = however
         long the receipt had left to live

    Check 4 is the one that matters most: an orchestrator holding a
    legitimate ALLOW for one resource cannot spend it on another, because
    this verifier never trusts a digest the caller merely states.
    """
    if not token:
        raise ReceiptInvalid("no receipt token presented")

    # Step 1: signature -- fully enforced by jwt.decode below. Step 2 ("not
    # expired") is deliberately NOT delegated to PyJWT's own wall-clock
    # `exp` check (`verify_exp` is off here): this function checks expiry
    # itself, against `now`, which is the only way a verifier can ever be
    # asked "is this expired AS OF the moment I am checking" -- real time by
    # default, an injected time in a test -- rather than "as of literally
    # whenever the interpreter happens to run". A bad signature still fails
    # here regardless, since only `verify_exp` is relaxed.
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[SIGNING_ALGORITHM],
            options={"verify_exp": False, "require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise ReceiptInvalid(f"receipt rejected: {exc}") from None

    receipt = DecisionReceipt.from_claims(claims)

    # Step 2, continued: the signature verified, so `receipt` below is
    # exactly as trustworthy whether or not it turns out to be expired --
    # attach it to ReceiptExpired so a caller can still attribute the
    # resulting denial to the right task (mirrors
    # `app/capability/tokens.py::CapabilityExpired.capability`).
    reference_now = now or datetime.now(timezone.utc)
    if receipt.expires_at <= reference_now:
        raise ReceiptExpired(
            "receipt has expired; receipts live seconds and there is no renewal",
            receipt=receipt,
        )

    # Step 3: operation match.
    if receipt.operation != operation:
        raise ReceiptInvalid(
            f"receipt authorizes {receipt.operation!r}, not {operation!r}"
        )

    # Step 4: digest recomputed over the resource THIS verifier received --
    # never trusted from the token or from the caller's argument.
    actual_digest = resource_digest(resource)
    if receipt.resource_digest != actual_digest:
        raise ReceiptInvalid(
            "receipt resource_digest does not match the resource presented "
            "for verification; a valid receipt for a different resource "
            "cannot be spent here"
        )

    # Step 5: single use.
    if seen_nonces.seen(receipt.nonce):
        raise ReceiptInvalid("receipt nonce has already been used")

    ttl_remaining = (receipt.expires_at - reference_now).total_seconds()
    seen_nonces.record(receipt.nonce, ttl_remaining)

    return receipt


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "TOKEN_TYPE",
    "SIGNING_ALGORITHM",
    "new_decision_id",
    "new_nonce",
    "ResourceLike",
    "resource_digest",
    "ReceiptInvalid",
    "ReceiptExpired",
    "DecisionReceipt",
    "sign_receipt",
    "verify_receipt",
    "NonceStore",
    "InMemoryNonceStore",
]
