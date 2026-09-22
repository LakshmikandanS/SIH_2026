"""The audit chain's hash-chaining algorithm: `compute_row_hash` and `verify()`.

Pure -- no Postgres import, no I/O, no subprocess -- on purpose, so it is testable
with an in-memory fake in the same breath as the real Postgres-backed audit_log
(packages/platform/AGENTS.md: "the legal record... append-only, hash-chained,
exactly one logical writer"). The one-logical-writer *guarantee* and the
tamper-*prevention* live in the schema (packages/platform/migrations/
0001_audit_chain.up.sql's advisory-lock trigger and append-only triggers), proven
directly against real Postgres in test_audit_chain_schema.py -- this module is the
recomputation both that schema and any reader of it must agree on, not a
reimplementation of the trigger for its own sake. Byte-for-byte agreement between
this module's `compute_row_hash` and the trigger's SQL is confirmed empirically in
test_audit_chain_schema.py::test_hash_matches_the_trigger_exactly, not assumed
because the two look similar.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

#: The predecessor of the first row in any chain: 32 zero bytes, matching
#: migration 0001's `decode(repeat('00', 32), 'hex')`.
GENESIS_HASH = b"\x00" * 32

_HASH_LENGTH = 32  # SHA-256 digest size, in bytes -- matches audit_log's CHECK constraint


@dataclass(frozen=True)
class ChainRow:
    """One row of `audit_log`, exactly as read back in `seq` order -- the fields
    `compute_row_hash` covers, in the order it covers them, plus `seq` (for error
    reporting only; it plays no part in the hash itself, which is by construction:
    the chain's integrity must not depend on which numbers a reader happened to see,
    only on each row's own recorded predecessor).

    `occurred_at_text` and `payload_text` are the exact, already-canonical strings
    that were hashed at write time -- not a `datetime` or a `dict` this class would
    have to re-serialize identically to what Postgres stored (see migration 0001's
    header comment for why that ambiguity is exactly what this split avoids).
    """

    seq: int
    event_name: str
    occurred_at_text: str
    actor_id: Optional[str]
    payload_text: str
    prev_hash: bytes
    row_hash: bytes


@runtime_checkable
class ChainSource(Protocol):
    """Anything that can hand back the audit chain in seq order. The real
    implementation (citadel_platform.audit.postgres.PostgresChainSource) queries
    Postgres; tests use a plain list of ChainRow. Structural typing (no inheritance
    required) so a test fake needs nothing from this module but the shape.
    """

    def rows(self) -> Sequence[ChainRow]: ...


@dataclass(frozen=True)
class ChainVerificationResult:
    """The outcome of `verify()`. `first_break_seq`/`reason` are both `None` exactly
    when `ok` is `True` -- there is no partially-valid result to interpret."""

    ok: bool
    first_break_seq: Optional[int]
    reason: Optional[str]


def compute_row_hash(
    *,
    prev_hash: bytes,
    event_name: str,
    occurred_at_text: str,
    actor_id: Optional[str],
    payload_text: str,
) -> bytes:
    """The exact SHA-256 digest migration 0001's `audit_log_chain()` trigger
    computes: `sha256(prev_hash || event_name || occurred_at_text ||
    (actor_id or '') || payload_text)`, each text field UTF-8 encoded. Written once
    here so a writer (citadel_platform.audit.postgres.append -- which does not call
    this; the trigger computes the stored hash) and a verifier never independently
    guess at the format and drift apart.
    """
    hasher = hashlib.sha256()
    hasher.update(prev_hash)
    hasher.update(event_name.encode("utf-8"))
    hasher.update(occurred_at_text.encode("utf-8"))
    hasher.update((actor_id or "").encode("utf-8"))
    hasher.update(payload_text.encode("utf-8"))
    return hasher.digest()


def verify(rows: Sequence[ChainRow]) -> ChainVerificationResult:
    """Recompute the whole chain from `rows` (already in seq order) and report the
    first break, if any.

    A "break" is either:
      - a bad *link*: `rows[i].prev_hash` does not equal `rows[i-1]`'s `row_hash`
        (or, for `rows[0]`, does not equal `GENESIS_HASH`) -- a row inserted,
        deleted, or reordered out of band; or
      - a bad *digest*: `rows[i].row_hash` does not match what `compute_row_hash`
        derives from `rows[i]`'s own recorded fields -- its content was edited in
        place after the fact.

    Both are "tampered with directly in the database" (docs/PLAN-M0.md task 6's
    "Done"): the trigger only ever runs on INSERT, so normal operation cannot
    produce either shape by itself -- see test_audit_chain_schema.py for the
    empirical proof that disabling the trigger and editing a row is exactly what it
    takes, and that this function catches it.
    """
    expected_prev = GENESIS_HASH
    for row in rows:
        if len(row.prev_hash) != _HASH_LENGTH or len(row.row_hash) != _HASH_LENGTH:
            return ChainVerificationResult(
                ok=False, first_break_seq=row.seq, reason=f"hash is not {_HASH_LENGTH} bytes"
            )
        if row.prev_hash != expected_prev:
            return ChainVerificationResult(
                ok=False,
                first_break_seq=row.seq,
                reason="broken link: prev_hash does not match the previous row's row_hash",
            )
        recomputed = compute_row_hash(
            prev_hash=row.prev_hash,
            event_name=row.event_name,
            occurred_at_text=row.occurred_at_text,
            actor_id=row.actor_id,
            payload_text=row.payload_text,
        )
        if recomputed != row.row_hash:
            return ChainVerificationResult(
                ok=False,
                first_break_seq=row.seq,
                reason="row_hash does not match this row's own recorded content -- edited in place",
            )
        expected_prev = row.row_hash

    return ChainVerificationResult(ok=True, first_break_seq=None, reason=None)
