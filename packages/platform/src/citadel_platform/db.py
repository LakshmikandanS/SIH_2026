"""Row-level SQL access for every package above `platform`.

One small surface -- `Database.query` / `.query_one` / `.execute` / `.script` -- over
the same `psql` subprocess the migrations runner and the audit client already use
(see `_psql.py`'s docstring for why `psql` and not a Python driver). Everything the
knowledge, runtime, deliverables and sovereignty packages persist goes through here,
so there is exactly one place that decides how a Python value becomes SQL text.

## How values become SQL

Statements are written with psycopg-style `%(name)s` placeholders and rendered with
`literal()` before they reach `psql`. That is client-side binding, done the way
psycopg2 does it: every value is emitted as a SQL *literal*, never spliced in as SQL
syntax. Strings are single-quoted with embedded quotes doubled, which is exactly
sufficient under `standard_conforming_strings = on` -- and every script sent from
here sets that first, so the guarantee does not depend on server configuration.
Identifiers are never parameters: a table or column name in these statements is
always a constant in the source.

`psql -v` variables (what `_psql.run_psql_csv` uses) were not reused here on purpose:
each one is a command-line argument, and Linux refuses a single argument over 128 KB
(`MAX_ARG_STRLEN`). A page of OCR text, a batch of chunk inserts or an embedding
vector would hit that. Rendering into the script, which travels over stdin, has no
such ceiling.

## How rows come back

Every query is wrapped as `WITH __q AS MATERIALIZED (<sql>) SELECT row_to_json(__q)
FROM __q`, and each output line is parsed as JSON. That keeps SQL `NULL` distinct from
an empty string (CSV cannot), returns numbers, booleans, arrays and JSONB as real
Python values, and works unchanged for `INSERT ... RETURNING`. `MATERIALIZED` keeps
the inner `ORDER BY`'s row order: the outer scan reads the tuplestore in the order it
was filled.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from citadel_platform._psql import PsqlError


@dataclass(frozen=True)
class Json:
    """Render a Python value as a `jsonb` literal."""

    value: Any


@dataclass(frozen=True)
class Vector:
    """Render a sequence of floats as a pgvector `vector` literal."""

    values: Sequence[float]


@dataclass(frozen=True)
class TextArray:
    """Render a sequence of strings as a `text[]` literal."""

    values: Sequence[str]


@dataclass(frozen=True)
class RealArray:
    """Render a sequence of floats as a `real[]` literal (bounding boxes)."""

    values: Sequence[float]


_PLACEHOLDER = re.compile(r"%\((\w+)\)s|%%")


def _quote(text: str) -> str:
    # NUL cannot be stored in a Postgres text value at all; an OCR engine or a PDF text
    # layer occasionally produces one. Dropping it is the only lossless-enough option
    # that does not fail the whole insert.
    cleaned = text.replace("\x00", "")
    return "'" + cleaned.replace("'", "''") + "'"


def _finite(value: float) -> float:
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"cannot store non-finite float {value!r}")
    return value


def literal(value: Any) -> str:
    """The one function that turns a Python value into SQL literal text."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(_finite(value))
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::bytea"
    if isinstance(value, datetime):
        return _quote(value.isoformat()) + "::timestamptz"
    if isinstance(value, date):
        return _quote(value.isoformat()) + "::date"
    if isinstance(value, Json):
        return _quote(json.dumps(value.value, ensure_ascii=False, default=str)) + "::jsonb"
    if isinstance(value, Vector):
        body = ",".join(repr(_finite(float(v))) for v in value.values)
        return _quote(f"[{body}]") + "::vector"
    if isinstance(value, RealArray):
        if not value.values:
            return "'{}'::real[]"
        return "ARRAY[" + ",".join(repr(_finite(float(v))) for v in value.values) + "]::real[]"
    if isinstance(value, TextArray) or (
        isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
    ):
        items: Sequence[str] = value.values if isinstance(value, TextArray) else value
        if not items:
            return "'{}'::text[]"
        return "ARRAY[" + ",".join(_quote(v) for v in items) + "]::text[]"
    raise TypeError(f"no SQL literal form for {type(value).__name__}: {value!r}")


def render(sql: str, params: Optional[Mapping[str, Any]] = None) -> str:
    """Replace every `%(name)s` with `literal(params[name])`; `%%` becomes `%`.

    A placeholder with no matching parameter is an error, not an empty string --
    a statement silently missing a value is how a WHERE clause stops filtering.
    """
    values = params or {}

    def substitute(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name is None:
            return "%"
        if name not in values:
            raise KeyError(f"SQL placeholder %({name})s has no parameter")
        return literal(values[name])

    return _PLACEHOLDER.sub(substitute, sql)


_PSQL_ARGS = ["psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"]
_PREAMBLE = "SET standard_conforming_strings = on;\nSET client_encoding = 'UTF8';\n"


@dataclass(frozen=True)
class Database:
    """A handle on one Postgres database, identified entirely by `PG*` variables.

    Stateless: each call is one `psql` process and one connection. Callers that need
    several statements to succeed or fail together use `script()`, which runs them in
    a single transaction.
    """

    env: Mapping[str, str]

    def _run(self, script: str) -> str:
        env = dict(self.env)
        env.setdefault("PGCLIENTENCODING", "UTF8")
        result = subprocess.run(
            _PSQL_ARGS, input=_PREAMBLE + script, capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            raise PsqlError(f"psql failed:\n{result.stderr.strip()}")
        return result.stdout

    def query(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
        """Rows as dicts. Works for SELECT and for DML with RETURNING."""
        statement = render(sql, params).strip().rstrip(";")
        wrapped = (
            f"WITH __q AS MATERIALIZED (\n{statement}\n) SELECT row_to_json(__q) FROM __q;\n"
        )
        rows: list[dict[str, Any]] = []
        for line in self._run(wrapped).splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    def query_one(
        self, sql: str, params: Optional[Mapping[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """The first column of the first row, or None."""
        row = self.query_one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()), None)

    def execute(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> None:
        """One statement, no result wanted."""
        self._run(render(sql, params).strip().rstrip(";") + ";\n")

    def script(self, statements: Iterable[tuple[str, Optional[Mapping[str, Any]]]]) -> None:
        """Several statements in ONE transaction: all commit or none do.

        `ON_ERROR_STOP` makes psql exit at the first failure without reaching
        COMMIT, and the server rolls the open transaction back when the connection
        closes.
        """
        parts = ["BEGIN;"]
        for sql, params in statements:
            parts.append(render(sql, params).strip().rstrip(";") + ";")
        parts.append("COMMIT;")
        self._run("\n".join(parts) + "\n")


__all__ = [
    "Database",
    "Json",
    "Vector",
    "TextArray",
    "RealArray",
    "literal",
    "render",
    "PsqlError",
]
