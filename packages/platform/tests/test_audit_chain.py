"""citadel_platform.audit.chain -- the hash-chaining algorithm, tested entirely with
an in-memory list of ChainRow. No Postgres, no subprocess: the schema-level
guarantees (one logical writer under real concurrency, append-only enforcement) are
proven separately, against real Postgres, in test_audit_chain_schema.py. What
belongs here is the pure question "given these rows, is the chain intact" -- and,
following this repo's own rule that a detector ships with a negative control it must
catch (tests/structural/AGENTS.md), every way `verify()` should say "no" gets its own
test, not just the happy path.
"""

from __future__ import annotations

from citadel_platform.audit.chain import GENESIS_HASH, ChainRow, compute_row_hash, verify


def _row(
    seq: int,
    prev_hash: bytes,
    *,
    event_name: str = "policy.decision",
    occurred_at_text: str = "2026-09-22T10:00:00.000000+00:00",
    actor_id: str | None = "user-1",
    payload_text: str = '{"a":1}',
) -> ChainRow:
    row_hash = compute_row_hash(
        prev_hash=prev_hash,
        event_name=event_name,
        occurred_at_text=occurred_at_text,
        actor_id=actor_id,
        payload_text=payload_text,
    )
    return ChainRow(
        seq=seq,
        event_name=event_name,
        occurred_at_text=occurred_at_text,
        actor_id=actor_id,
        payload_text=payload_text,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )


def _valid_chain(n: int) -> list[ChainRow]:
    rows = []
    prev = GENESIS_HASH
    for seq in range(1, n + 1):
        row = _row(seq, prev, payload_text=f'{{"i":{seq}}}')
        rows.append(row)
        prev = row.row_hash
    return rows


# ---------------------------------------------------------------------------
# compute_row_hash: pinned so a future refactor can't silently change the format
# ---------------------------------------------------------------------------


def test_compute_row_hash_matches_a_hand_computed_vector():
    # Independently computable: sha256(32 zero bytes || "policy.decision" ||
    # "2026-09-22T10:00:00.000000+00:00" || "user-1" || '{"a":1}'), all UTF-8.
    expected = bytes.fromhex("d7b9a125ac0c01fd79f51d0b871c29b0043d88aa9c0c74fcd294a157a8f986ac")
    actual = compute_row_hash(
        prev_hash=GENESIS_HASH,
        event_name="policy.decision",
        occurred_at_text="2026-09-22T10:00:00.000000+00:00",
        actor_id="user-1",
        payload_text='{"a":1}',
    )
    assert actual == expected


def test_compute_row_hash_treats_none_actor_id_the_same_as_empty_string():
    with_none = compute_row_hash(
        prev_hash=GENESIS_HASH, event_name="e", occurred_at_text="t", actor_id=None, payload_text="{}"
    )
    with_empty = compute_row_hash(
        prev_hash=GENESIS_HASH, event_name="e", occurred_at_text="t", actor_id="", payload_text="{}"
    )
    assert with_none == with_empty


def test_compute_row_hash_is_sensitive_to_every_field():
    base = dict(prev_hash=GENESIS_HASH, event_name="e", occurred_at_text="t", actor_id="a", payload_text="{}")
    baseline = compute_row_hash(**base)  # type: ignore[arg-type]
    assert compute_row_hash(**{**base, "event_name": "different"}) != baseline  # type: ignore[arg-type]
    assert compute_row_hash(**{**base, "occurred_at_text": "different"}) != baseline  # type: ignore[arg-type]
    assert compute_row_hash(**{**base, "actor_id": "different"}) != baseline  # type: ignore[arg-type]
    assert compute_row_hash(**{**base, "payload_text": '{"different":true}'}) != baseline  # type: ignore[arg-type]
    assert compute_row_hash(**{**base, "prev_hash": b"\x01" * 32}) != baseline  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# verify(): the happy path
# ---------------------------------------------------------------------------


def test_verify_accepts_an_empty_chain():
    assert verify([]) == verify([])  # trivially ok; see next assertion for the real check
    result = verify([])
    assert result.ok is True
    assert result.first_break_seq is None


def test_verify_accepts_a_single_correctly_chained_row():
    result = verify(_valid_chain(1))
    assert result.ok is True


def test_verify_accepts_a_long_correctly_chained_sequence():
    result = verify(_valid_chain(50))
    assert result.ok is True
    assert result.reason is None


# ---------------------------------------------------------------------------
# verify(): negative controls -- every way this function must say "no"
# ---------------------------------------------------------------------------


def test_verify_detects_a_tampered_payload():
    """The core claim of a hash chain: editing a row's content in place, without
    touching its stored row_hash, must be caught -- this is exactly what
    ALTER TABLE ... DISABLE TRIGGER + UPDATE looks like at the schema level
    (test_audit_chain_schema.py), reproduced here without a database."""
    rows = _valid_chain(5)
    tampered = rows[2]
    rows[2] = ChainRow(
        seq=tampered.seq,
        event_name=tampered.event_name,
        occurred_at_text=tampered.occurred_at_text,
        actor_id=tampered.actor_id,
        payload_text='{"i":"tampered"}',
        prev_hash=tampered.prev_hash,
        row_hash=tampered.row_hash,  # stale: still the hash of the ORIGINAL payload_text
    )

    result = verify(rows)

    assert result.ok is False
    assert result.first_break_seq == tampered.seq
    assert result.reason is not None and "edited in place" in result.reason


def test_verify_detects_a_deleted_row_as_a_broken_link():
    rows = _valid_chain(5)
    del rows[2]  # seq 1, 2, 4, 5 remain -- row 4's prev_hash points at the missing row 3

    result = verify(rows)

    assert result.ok is False
    assert result.first_break_seq == 4
    assert result.reason is not None and "broken link" in result.reason


def test_verify_detects_reordered_rows():
    rows = _valid_chain(4)
    rows[1], rows[2] = rows[2], rows[1]

    result = verify(rows)

    assert result.ok is False
    assert result.reason is not None and "broken link" in result.reason


def test_verify_detects_a_first_row_with_the_wrong_genesis_predecessor():
    rows = _valid_chain(1)
    bad_genesis = ChainRow(
        seq=rows[0].seq,
        event_name=rows[0].event_name,
        occurred_at_text=rows[0].occurred_at_text,
        actor_id=rows[0].actor_id,
        payload_text=rows[0].payload_text,
        prev_hash=b"\x99" * 32,
        row_hash=rows[0].row_hash,
    )

    result = verify([bad_genesis])

    assert result.ok is False
    assert result.first_break_seq == 1
    assert result.reason is not None and "broken link" in result.reason


def test_verify_detects_a_short_hash():
    rows = _valid_chain(1)
    truncated = ChainRow(
        seq=rows[0].seq,
        event_name=rows[0].event_name,
        occurred_at_text=rows[0].occurred_at_text,
        actor_id=rows[0].actor_id,
        payload_text=rows[0].payload_text,
        prev_hash=rows[0].prev_hash,
        row_hash=rows[0].row_hash[:16],
    )

    result = verify([truncated])

    assert result.ok is False
    assert result.reason is not None and "32 bytes" in result.reason


def test_verify_stops_at_the_first_break_not_the_last():
    rows = _valid_chain(5)
    tampered_early = rows[1]
    rows[1] = ChainRow(
        seq=tampered_early.seq,
        event_name=tampered_early.event_name,
        occurred_at_text=tampered_early.occurred_at_text,
        actor_id=tampered_early.actor_id,
        payload_text="{}",
        prev_hash=tampered_early.prev_hash,
        row_hash=tampered_early.row_hash,
    )
    # Row 4's prev_hash now also silently disagrees, as a knock-on effect -- verify()
    # must report the ROOT cause (seq 2), not the first symptom it happens to scan
    # into if it kept going past the first break.

    result = verify(rows)

    assert result.ok is False
    assert result.first_break_seq == 2
