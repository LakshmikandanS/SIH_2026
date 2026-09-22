"""`citadel_api` -- the M0 checkpoint's API service: wires the identity,
registry, policy-evaluator and audit-chain pieces `packages/` already
implements into a real running process with a real HTTP surface (PLAN-M0
task 10, pulled forward of task 9's tracing/metrics on Fahim's explicit
request for a working, demonstrable checkpoint before further pipeline
work). `web/src` is this service's UI, served from the same process -- see
`app.py`'s module docstring for how and why.

Two substitutions from what `services/AGENTS.md` and PLAN-M0 task 10
originally named, both sandbox-driven and both documented in full where
they actually happen, not just here:

  * Starlette + uvicorn, not FastAPI -- `app.py`'s module docstring.
  * `psql` subprocess calls, not `psycopg` -- already true of
    `citadel_platform.audit.psql_client` and `.identity.store`, which this
    service simply reuses; see those modules' own docstrings.

Neither changes the architecture `services/AGENTS.md` describes -- a thin
service wiring packages together, owning transport and nothing else -- and
both are named, temporary, and reversible the moment this runs on the real
WSL2 machine (ADR-0005) with real internet access.

A real `uv` workspace member: `services/api/pyproject.toml` declares
`starlette`/`uvicorn` as actual dependencies (not an assumption that they
happen to be pre-installed) and root `pyproject.toml`'s
`[tool.uv.workspace]` lists `services/api` alongside `packages/*`. `uv sync`
resolves and installs it like any other member -- run it again after
pulling this change, since it touches the lockfile. `scripts/lib/env.sh`'s
sandbox-bridge fallback (a `PYTHONPATH` bridge, for a machine with no
network to resolve the workspace at all) still applies to this package the
same as every other one, with `services/*/src` in that file's mypy target
discovery so `scripts/check.sh` type-checks it either way.
"""

from __future__ import annotations
