"""Session-signing key lifecycle for `citadel_contracts.identity`.

See `citadel_platform.identity.keys` for the design rationale and
`citadel_platform.identity.cli` for the `python -m citadel_platform.identity`
command line.
"""

from __future__ import annotations

from citadel_platform.identity.keys import (
    ENV_VAR,
    SessionSigningKeyError,
    generate_key_material,
    load_session_signing_key,
)

__all__ = [
    "ENV_VAR",
    "SessionSigningKeyError",
    "generate_key_material",
    "load_session_signing_key",
]
