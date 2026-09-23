# AGENTS.md — services

The deployable processes. These may import any package; nothing imports them.

| Service | Runs | What it is |
|---|---|---|
| `api` | App box | Starlette + uvicorn. Auth, task submit/cancel/probe, SSE task streams, documents and uploads, artifacts and approval decisions, models and routing, sovereignty, audit, metrics — and the web UI (`web/src/`) mounted on the same origin |
| `worker` | App box | Task threads (claim with `SKIP LOCKED`, run the agent loop, write the journal), ingestion threads behind CPU admission, resident-set warm-up, and — with `CITADEL_SEED_CORPUS=1` — the demo corpus, once the approved models are installed |
| `sandbox` | App box | One hardened service on an internal-only network that verifies a receipt and runs model-authored Python ([ADR-0007](../docs/adr/0007-the-sandbox-is-a-service-not-a-container-per-run.md)) |

All three run from one image (`ops/compose/Dockerfile`) and one Compose file
(`ops/compose/docker-compose.yml`), started by `citadel.cmd` on Windows or `scripts/up.sh`
on Linux/WSL2; `scripts/run.sh` runs `api` and `worker` as plain processes (with the
in-process sandbox, labelled `kind: process` wherever it reports) for development.

**Starlette + uvicorn, not FastAPI.** FastAPI could not be installed in the sandbox this
was first built in — PyPI is network-blocked there and only `starlette`/`uvicorn`/`flask`
were pre-installed (confirmed empirically). Starlette is FastAPI's own foundation; this is
a substitution of implementation, not of the architecture this file describes, and it is
named again in `citadel_api`'s own package docstring. Revisit once this is built somewhere
with a real package registry. The container image installs its third-party dependencies
from `ops/compose/requirements.txt` at build time and puts `packages/*/src` and
`services/*/src` on `PYTHONPATH`, the same bridge `scripts/lib/env.sh` uses, so no build
backend has to be fetched for the workspace packages themselves.

**Real `uv` workspace members.** Root `pyproject.toml` lists `services/api`,
`services/worker` and `services/sandbox`, and each declares its real third-party
dependencies — a `.venv` built by `uv sync` must contain everything the service imports.

`python -m citadel_worker missing-models` answers "which approved models does the runtime
lack" from the registry and the runtime alone (exit 2: the runtime is not answering); the
launchers ask it rather than guessing.

## Thin by design

A service wires packages together and owns transport. It does not own logic. If you are
writing a rule, a policy, a score or a decision inside `services/`, it belongs in a package
and the service should be calling it.

This is what makes the eventual split into separate deployables a deployment change rather
than a rewrite — the handoff's ADR-007 judgement, which still holds.

## Identity

`api` derives identity from a **verified session token**, never from a request body, and
the discard of any caller-supplied identity field is **visible in the code** rather than
implied. Keep that habit from the prototype: an explicit discard is the thing a reviewer
can point at.

## The GPU box runs nothing from here

It runs the inference runtime and nothing else. That is what makes its firewall rule
readable in five seconds (see `packages/sovereignty/AGENTS.md`).
