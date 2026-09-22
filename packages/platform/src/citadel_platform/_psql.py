"""A tiny, shared `psql`-subprocess runner for reading and writing plain rows.

Private (leading underscore: not part of this package's public API). Two call
sites need it -- `citadel_platform.audit.psql_client` and
`citadel_platform.identity.store` -- and both want the same thing: run one
statement, get rows back as plain strings, fail loudly with `psql`'s own
stderr on error. Neither is `citadel_platform.migrations.runner`, which has
its own private `_run_psql` shaped around *scripts* (`-f -`, multi-statement,
`--single-transaction`) rather than one-row-back queries, and which this
module deliberately does not import: `migrations` and `audit`/`identity` are
different subsystems that happen to reach for the same underlying tool, not
a shared dependency either should have on the other.

Exists at all only because this sandbox has no installable `psycopg` (see
`citadel_platform.audit.postgres`'s module docstring) -- the real WSL2 machine
does, and that module's psycopg-based writer/reader remain the intended
production path there. This is the same substitution `migrations.runner`
already made for the same reason, extended to the two other places this
package talks to Postgres.
"""

from __future__ import annotations

import csv
import io
import subprocess
from typing import Mapping, Optional


class PsqlError(Exception):
    """`psql` exited non-zero. Always carries its stderr, never swallowed."""


def run_psql_csv(
    sql: str,
    *,
    env: Mapping[str, str],
    variables: Optional[Mapping[str, str]] = None,
) -> list[list[str]]:
    """Run one SQL statement, return its result as parsed CSV rows --
    `[header, *data_rows]`, each a list of column strings, or `[]` for a
    statement with no result set. `--csv` (not `-t -A`) specifically because
    `payload_text` and `display_name` can contain commas, quotes and
    newlines; CSV quoting handles that correctly where naive splitting on a
    delimiter would silently corrupt a row. `variables` become `psql -v
    name=value`, referenced from `sql` as `:'name'` -- psql's own safe-quoting
    substitution, the same convention `citadel_platform.migrations.runner`
    uses and for the same reason: a value never needs to be trusted as
    injection-safe by construction alone.
    """
    # -f - (a script read from stdin), not -c: psql's `:'name'` variable
    # substitution is a script/meta-command feature and is NOT applied to a
    # -c argument -- confirmed empirically (a `:'name'` sent via -c reaches
    # the backend parser literally and fails with "syntax error at or near
    # ':'"). citadel_platform.migrations.runner._run_psql uses -f - for the
    # identical reason; this is that same, already-proven shape.
    #
    # -q (quiet): -f/script mode otherwise appends its own command-completion
    # tag ("INSERT 0 1") as a trailing line after the --csv result set --
    # confirmed empirically, not assumed -- which a plain CSV parse cannot
    # tell apart from a genuine data row of one column. -t/-A (migrations.
    # runner's own flags) suppress this a different way; -q is the --csv
    # equivalent.
    args = ["psql", "--csv", "-q", "-v", "ON_ERROR_STOP=1"]
    for key, value in (variables or {}).items():
        args += ["-v", f"{key}={value}"]
    args += ["-f", "-"]

    result = subprocess.run(args, input=sql, capture_output=True, text=True, env=dict(env))
    if result.returncode != 0:
        raise PsqlError(f"psql failed:\n{result.stderr.strip()}")

    stdout = result.stdout.strip("\n")
    if not stdout:
        return []
    return list(csv.reader(io.StringIO(stdout)))


def run_psql_statement(
    sql: str,
    *,
    env: Mapping[str, str],
    variables: Optional[Mapping[str, str]] = None,
) -> None:
    """Run one SQL statement whose result (if any) is not needed. Thin
    wrapper over `run_psql_csv` so callers that only want the side effect
    (an INSERT with no `RETURNING`) don't have to spell out and discard a
    return value.
    """
    run_psql_csv(sql, env=env, variables=variables)


def hex_to_bytes(value: str) -> bytes:
    """`encode(col, 'hex')`'s output (plain hex, no `\\x` prefix) back to
    `bytes`. Kept here, not re-derived at each call site, so every reader of
    a `bytea` column read this way agrees on the one format
    `PsqlChainSource` asks Postgres to emit it in.
    """
    return bytes.fromhex(value)


__all__ = ["PsqlError", "run_psql_csv", "run_psql_statement", "hex_to_bytes"]
