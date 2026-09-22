# AGENTS.md — services

The deployable processes. These may import any package; nothing imports them.

| Service | Runs | Notes |
|---|---|---|
| `api` | App box | Starlette today (see "M0 checkpoint" below), FastAPI once installable. Auth, task submit/cancel, SSE streams, artifacts, admin |
| `worker` | App box | Pulls from the queue, runs the agent loop, writes the journal -- not yet started |
| `sandbox` | App box | One-shot container image. `--network none`, capped, no host mounts, destroyed after use -- not yet started |

## M0 checkpoint: what `api` actually is right now

PLAN-M0 task 10, pulled forward of task 9 (tracing/metrics) on Fahim's explicit request
for a working, demonstrable checkpoint before further pipeline work: `services/api`
(`citadel_api`) is real and running, wiring together everything `packages/` already had
built -- the registry loader, session identity, the policy evaluator, and the hash-chained
audit log -- into one Starlette app with real routes and a real web UI (`web/`) mounted on
it, same origin. `scripts/run-api.sh` starts it end to end (dev database, migrate the demo
data, launch uvicorn).

**Starlette + uvicorn, not FastAPI.** FastAPI cannot be installed in the sandbox this was
first built in -- PyPI is network-blocked there and only `starlette`/`uvicorn`/`flask` are
pre-installed (confirmed empirically, not assumed). Starlette is FastAPI's own foundation
and keeps this table's "SSE streams" line reachable without a rewrite; this is a
substitution of implementation, not of the architecture this file describes, and it is
named again in `citadel_api`'s own package docstring, not only here. Revisit once this runs
somewhere with a real package registry.

**Not yet a `uv` workspace member.** Root `pyproject.toml`'s `[tool.uv.workspace]` still
lists only `packages/*`; `citadel_api` is reached through the same sandbox `PYTHONPATH`
bridge as everything else (`scripts/lib/env.sh`, with `services/*/src` added to its mypy
target discovery so `scripts/check.sh` still type-checks it). Joining the workspace for
real -- so it can declare `starlette`/`uvicorn` as actual dependencies -- is follow-up work.

**What is built:** `GET /api/health`, `GET /api/registry/tools`, `GET /api/demo/users`,
`POST /api/auth/session` (issues a session for one of the three seeded demo identities --
no password exists yet, see `citadel_api.handlers.create_session`'s own docstring for why
this is not an invariant-3 violation), `GET /api/me`, `POST /api/policy/try` (the ACL
demonstration: evaluates the real `registry/policy.yaml` rules against the caller's
verified identity and a caller-described resource, and writes a real audit event for the
decision), `GET /api/audit/recent`, `GET /api/audit/verify`.

**What is deliberately not built yet, visibly, not silently:** task/agent submission,
cancellation, and the SSE stream that would carry their progress (there is no task system
yet for it to stream); artifacts and admin endpoints; the `worker` and `sandbox` services
in the table above; the tool chokepoint itself (`packages/tools/AGENTS.md`) and receipt
issuance, which is why `try_policy` always evaluates with `receipt.valid=False` and takes
over that one "record the decision" responsibility from the not-yet-built chokepoint in
the meantime, explicitly, in its own docstring.

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
