"""A second, `psql`-subprocess `ChainSource` and `append`, for reading and
writing the real `audit_log` table where `psycopg` cannot be installed.

`citadel_platform.audit.postgres` is, and remains, the intended production
path on the real WSL2 machine: psycopg is the right choice for a table an
API process writes to on every policy decision, and that module's own
docstring already explains why this dev sandbox cannot even import it
(`ModuleNotFoundError: psycopg`, and PyPI is network-blocked here). This
module exists so that fact does not also block *this* sandbox from ever
running the real audit chain against a real Postgres -- `psql` needs no
Python driver at all, the same reasoning `citadel_platform.migrations.
runner` already used for schema migrations. Everything downstream of a row
-- the hash format, `verify()` -- is `citadel_platform.audit.chain`, shared
unchanged by both writers: this module's only job is getting rows in and out
of Postgres, never re-deriving what a row means.

`_canonical_json`/`_format_occurred_at` are intentionally duplicated from
`citadel_platform.audit.postgres` rather than imported from it -- importing
that module in this sandbox fails at the `import psycopg` line before either
function would even be reachable. Both copies must stay byte-identical
(they define the exact text `citadel_platform.audit.chain.compute_row_hash`
hashes); a change to one demands the same change to the other, and
`test_psql_client_matches_postgres_formatting` (this package's tests) pins
that agreement so it cannot drift unnoticed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from citadel_platform._psql import hex_to_bytes, run_psql_csv
from citadel_platform.audit.chain import ChainRow


def _canonical_json(payload: Dict[str, Any]) -> str:
    """Must stay byte-identical to `citadel_platform.audit.postgres.
    canonical_json` -- see this module's own docstring."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _format_occurred_at(moment: datetime) -> str:
    """Must stay byte-identical to `citadel_platform.audit.postgres.
    format_occurred_at` -- see this module's own docstring."""
    if moment.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat()


_ROWS_SQL = (
    "SELECT seq, event_name, occurred_at_text, actor_id, payload_text, "
    "encode(prev_hash, 'hex') AS prev_hash_hex, encode(row_hash, 'hex') AS row_hash_hex "
    "FROM audit_log ORDER BY seq"
)


@dataclass(frozen=True)
class PsqlChainSource:
    """Reads `audit_log` back in seq order via `psql`. Implements
    `citadel_platform.audit.chain.ChainSource` structurally (that Protocol is
    `runtime_checkable` precisely so a second implementation like this one
    needs nothing from `citadel_platform.audit.postgres` -- not even an
    import of it, which would fail here regardless.
    """

    env: Mapping[str, str]

    def rows(self) -> Sequence[ChainRow]:
        parsed = run_psql_csv(_ROWS_SQL, env=self.env)
        if not parsed:
            return []
        header, *data_rows = parsed
        assert header == [
            "seq",
            "event_name",
            "occurred_at_text",
            "actor_id",
            "payload_text",
            "prev_hash_hex",
            "row_hash_hex",
        ], f"unexpected audit_log column order from psql: {header}"
        return [
            ChainRow(
                seq=int(seq),
                event_name=event_name,
                occurred_at_text=occurred_at_text,
                actor_id=actor_id or None,
                payload_text=payload_text,
                prev_hash=hex_to_bytes(prev_hash_hex),
                row_hash=hex_to_bytes(row_hash_hex),
            )
            for seq, event_name, occurred_at_text, actor_id, payload_text, prev_hash_hex, row_hash_hex in data_rows
        ]


def append_via_psql(
    env: Mapping[str, str],
    *,
    event_name: str,
    actor_id: Optional[str],
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = None,
) -> int:
    """Insert one audit event via `psql`, return the new row's `seq`.

    Mirrors `citadel_platform.audit.postgres.append()` exactly in what it
    sends: `event_name`/`occurred_at_text`/`actor_id`/`payload_text`, and
    nothing else -- migration 0001's trigger computes `seq`/`prev_hash`/
    `row_hash` itself, under the same advisory lock regardless of which
    client issued the INSERT, so this and the psycopg writer are equally
    safe to use concurrently against the same database.

    `actor_id` is written as a genuine SQL `NULL` when absent, not an empty
    string -- `psql -v` substitution has no NULL of its own (it substitutes
    text), so the two-branch SQL below is the honest way to keep "no actor"
    distinguishable from "actor is the empty string", matching what a
    psycopg `NULL` parameter would do.
    """
    moment = occurred_at if occurred_at is not None else datetime.now(timezone.utc)
    variables = {
        "event_name": event_name,
        "occurred_at_text": _format_occurred_at(moment),
        "payload_text": _canonical_json(payload),
    }
    if actor_id is not None:
        variables["actor_id"] = actor_id
        actor_sql = ":'actor_id'"
    else:
        actor_sql = "NULL"

    sql = (
        "INSERT INTO audit_log (event_name, occurred_at_text, actor_id, payload_text) "
        f"VALUES (:'event_name', :'occurred_at_text', {actor_sql}, :'payload_text') "
        "RETURNING seq"
    )
    parsed = run_psql_csv(sql, env=env, variables=variables)
    header, *data_rows = parsed
    assert header == ["seq"] and len(data_rows) == 1, f"unexpected RETURNING output: {parsed}"
    return int(data_rows[0][0])


__all__ = ["PsqlChainSource", "append_via_psql"]
