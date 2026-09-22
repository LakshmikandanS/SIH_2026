"""The audit chain: append-only, hash-chained, one logical writer (root AGENTS.md
invariant 8). citadel_platform.audit.chain is the pure algorithm; citadel_platform.
audit.postgres is the real, psycopg-backed writer and reader.
"""

from __future__ import annotations

from citadel_platform.audit.chain import (
    GENESIS_HASH,
    ChainRow,
    ChainSource,
    ChainVerificationResult,
    compute_row_hash,
    verify,
)

__all__ = [
    "GENESIS_HASH",
    "ChainRow",
    "ChainSource",
    "ChainVerificationResult",
    "compute_row_hash",
    "verify",
]
