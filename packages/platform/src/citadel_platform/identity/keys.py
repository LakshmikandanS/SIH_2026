"""Session-signing key lifecycle.

`citadel_contracts.identity` is a pure sign/verify primitive: it takes a key and
never decides where one comes from. Exactly like `receipts.py`'s own docstring
for its (still M3-M4, not-yet-built) signing key, "key material gets a
documented lifecycle" is named there as `citadel_platform`'s concern, not
`citadel_contracts`'s. Session tokens are M0 work (ADR-0001 §Q5), so unlike the
receipt-signing lifecycle, this one has to exist now, not merely be planned for.

The lifecycle, such as it is at M0: read a base64-encoded 32-byte Ed25519 private
key from `CITADEL_SESSION_SIGNING_KEY`, or fail loud-but-not-closed with a
generated per-process key when the variable is unset. "Fail closed" (root
AGENTS.md's general posture) would mean refusing to start at all -- wrong here,
because a demo box that will not boot without an operator first minting a key by
hand is a worse failure mode for M0 than a working demo whose sessions do not
survive a restart. The correct fail-closed *default* the porting note in
`packages/contracts/AGENTS.md` asks for is that this never falls back to
something an attacker can predict or influence (`Ed25519PrivateKey.generate()`
is a fresh, process-local, cryptographically random key every time, not a fixed
placeholder), while making the operational cost of that fallback -- restart the
process, invalidate every outstanding session -- loud rather than silent.
`generate_key_material` is the other half: how an operator actually produces a
value for that environment variable in the first place, so "set it" is not left
unanswered.
"""

from __future__ import annotations

import base64
import os
import warnings
from typing import Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: The one environment variable this lifecycle reads. Named `SESSION`, not
#: `IDENTITY`, so it is never confused with a future, separate receipt-signing
#: key variable -- citadel_contracts.identity's own docstring is explicit that
#: the two token kinds must never share a key.
ENV_VAR = "CITADEL_SESSION_SIGNING_KEY"

_RAW_KEY_LENGTH = 32  # Ed25519 private keys are always exactly 32 bytes.


class SessionSigningKeyError(ValueError):
    """`CITADEL_SESSION_SIGNING_KEY` is set but is not usable: not valid
    base64, or the wrong length once decoded. Deliberately NOT caught by
    falling back to a generated key -- an operator who set the variable and
    made a typo needs to see that, not have it silently ignored."""


def generate_key_material() -> str:
    """A freshly generated Ed25519 private key, base64-encoded exactly as
    `CITADEL_SESSION_SIGNING_KEY` expects it. What an operator runs once (via
    `python -m citadel_platform.identity genkey`) and stores as a secret --
    this function does not itself persist anything, matching
    `citadel_contracts.receipts.sign_receipt`'s own stance of being a pure
    primitive that trusts its caller to hold key material responsibly.
    """
    return base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode("ascii")


def load_session_signing_key(env: Optional[Mapping[str, str]] = None) -> Ed25519PrivateKey:
    """The one place a process decides which session-signing key it holds.

    `env` defaults to the real process environment; callers inject a plain
    dict in tests, the same convention `citadel_platform.migrations` uses for
    `PG*` variables, so no test needs to mutate `os.environ` to exercise
    either branch.
    """
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_VAR)

    if raw:
        try:
            key_bytes = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise SessionSigningKeyError(f"{ENV_VAR} is not valid base64: {exc}") from exc
        if len(key_bytes) != _RAW_KEY_LENGTH:
            raise SessionSigningKeyError(
                f"{ENV_VAR} must decode to exactly {_RAW_KEY_LENGTH} bytes (a raw Ed25519 "
                f"private key); got {len(key_bytes)}. Generate one with "
                f"'python -m citadel_platform.identity genkey'."
            )
        return Ed25519PrivateKey.from_private_bytes(key_bytes)

    warnings.warn(
        f"{ENV_VAR} is not set -- generating a random per-process session-signing key. "
        "Every session this process issues becomes unverifiable the moment this "
        "process restarts, and unverifiable to any OTHER process right now (a second "
        "worker would generate its own, different key). Fine for a single, short-lived "
        "dev process; wrong for anything the WSL2 demo box treats as long-running or "
        f"multi-process. Set {ENV_VAR} (see 'python -m citadel_platform.identity genkey') "
        "before then.",
        stacklevel=2,
    )
    return Ed25519PrivateKey.generate()


__all__ = [
    "ENV_VAR",
    "SessionSigningKeyError",
    "generate_key_material",
    "load_session_signing_key",
]
