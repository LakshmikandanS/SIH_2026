"""A scratch Postgres database for citadel_platform's integration tests.

Not named `conftest.py` on purpose, even though that's pytest's own convention for
shared fixtures: mypy resolves `packages/platform/tests/conftest.py` and
`tests/structural/conftest.py` to the SAME bare module name (no `tests/` directory
anywhere in this repo has an `__init__.py` -- see root pyproject.toml's
`--import-mode=importlib` comment) -- confirmed empirically, not assumed: mypy
refuses with "Duplicate module named 'conftest'" the moment a second one exists.
That is the identical collision this repo already hit and fixed for `tests` itself,
but this time no per-module override can resolve it, because the conflict is two
DIFFERENT files each wanting to BE the module `conftest`, not one file needing
different settings. `tests/structural/AGENTS.md` and this package's own convention
call for a globally unique basename per file; `conftest.py` cannot have one and
still be named `conftest.py`. So: a plain module with an ordinary, unique name,
imported explicitly by the handful of test files that need it, instead of a second
auto-loaded conftest.py.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from contextlib import contextmanager
from typing import Iterator

import pytest


def pg_reachable() -> bool:
    """Whether some Postgres answers at all, via PGHOST/PGPORT/libpq defaults."""
    try:
        return subprocess.run(["pg_isready", "-q"], env=os.environ.copy()).returncode == 0
    except FileNotFoundError:
        return False


@contextmanager
def pg_scratch_db() -> Iterator[dict[str, str]]:
    """A freshly created, empty scratch database, as an environment mapping ready to
    pass to citadel_platform.migrations (or `psql`/`subprocess` directly) -- dropped
    again on the way out, success or failure. Skips the calling test (via
    `pytest.skip`, which works from a plain helper exactly as it would from a
    fixture -- it's just an exception pytest's runner recognizes) with a clear,
    actionable reason if no Postgres is reachable at all.
    """
    if not pg_reachable():
        pytest.skip(
            "no Postgres reachable via PGHOST/PGPORT/libpq defaults -- "
            "run `scripts/dev-db.sh start` and export the PGHOST/PGPORT/PGUSER it prints"
        )

    db_name = f"citadel_test_{secrets.token_hex(8)}"
    base_env = os.environ.copy()
    subprocess.run(["createdb", db_name], env=base_env, check=True, capture_output=True)
    try:
        yield {**base_env, "PGDATABASE": db_name}
    finally:
        subprocess.run(["dropdb", "--if-exists", db_name], env=base_env, capture_output=True)
