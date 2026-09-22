"""The real, Postgres-backed audit chain writer and reader -- psycopg v3, written
for the target WSL2 machine.

Not executed in the dev sandbox this was first built in: no psycopg build is
importable there (root AGENTS.md's sandbox note) and none can be installed (PyPI is
network-blocked). Verified here by `mypy --strict` (under the sandbox's
`--ignore-missing-imports`, needed for this exact situation -- see scripts/lib/
env.sh's citadel::mypy) and by review against test_audit_chain_schema.py, which
proves the trigger this module's `append()` relies on -- concurrency, append-only
enforcement, tamper detection -- directly through `psql`, independent of this class.
Treat a change here as needing manual review against that proof until this module
itself can be run for real, and see test_audit_chain_postgres.py for the (currently
skipped, for the same reason) tests that would exercise this module directly once a
driver is available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from citadel_platform.audit.chain import ChainRow

if TYPE_CHECKING:
    import psycopg
else:
    import psycopg  # type: ignore[import-not-found]  # not installed in this sandbox; see module docstring


def canonical_json(payload: Dict[str, Any]) -> str:
    """The exact serialization `citadel_platform.audit.chain.compute_row_hash`'s
    `payload_text` covers. `sort_keys=True` makes the result independent of the
    caller's dict insertion order (Python dicts preserve insertion order; a JSON
    object does not promise to, so two logically identical payloads built in a
    different order must still hash identically). `separators` strips the default
    `", "`/`": "` whitespace so the bytes are stable across json module versions.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def format_occurred_at(moment: datetime) -> str:
    """The exact text `compute_row_hash`'s `occurred_at_text` covers: ISO-8601,
    always with an explicit UTC offset -- never a naive datetime, since "which
    timezone" left ambiguous is not acceptable in an audit record, and migration
    0001's `occurred_at_text::timestamptz` cast needs an offset to be unambiguous
    too.
    """
    if moment.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class PostgresChainSource:
    """Reads audit_log back in seq order. Implements citadel_platform.audit.chain.
    ChainSource structurally (via Protocol) -- no inheritance needed, so tests can
    use a plain list instead without importing this module (or psycopg) at all.
    """

    conn: "psycopg.Connection[Any]"

    def rows(self) -> Sequence[ChainRow]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT seq, event_name, occurred_at_text, actor_id, payload_text, prev_hash, row_hash "
                "FROM audit_log ORDER BY seq"
            )
            return [
                ChainRow(
                    seq=seq,
                    event_name=event_name,
                    occurred_at_text=occurred_at_text,
                    actor_id=actor_id,
                    payload_text=payload_text,
                    prev_hash=bytes(prev_hash),
                    row_hash=bytes(row_hash),
                )
                for seq, event_name, occurred_at_text, actor_id, payload_text, prev_hash, row_hash in cur.fetchall()
            ]


def append(
    conn: "psycopg.Connection[Any]",
    *,
    event_name: str,
    actor_id: Optional[str],
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = None,
) -> None:
    """Insert one audit event. Does NOT compute or pass prev_hash/row_hash/seq --
    migration 0001's `audit_log_chain()` trigger computes all three, inside the same
    advisory-lock-serialized transaction as every other writer, from exactly the
    event_name/occurred_at_text/actor_id/payload_text this function supplies. A
    writer that computed its own hash here, instead of letting the trigger do it,
    could race with a concurrent writer between "read the last hash" and "insert" --
    the one thing the trigger design in migration 0001 exists to make impossible.
    """
    moment = occurred_at if occurred_at is not None else datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (event_name, occurred_at_text, actor_id, payload_text) "
            "VALUES (%s, %s, %s, %s)",
            (event_name, format_occurred_at(moment), actor_id, canonical_json(payload)),
        )
    conn.commit()
