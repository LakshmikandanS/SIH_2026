# Citadel

A sovereign, air-gapped agentic AI workbench for confidential industrial work.

Refineries, PSUs, defence-linked manufacturers and government offices produce a great deal
of routine but sensitive knowledge work — approval notes, engineering calculations,
internal tooling, review of scanned drawings and inspection reports. None of it can go to a
cloud assistant. Citadel runs entirely on the organisation's own hardware, produces real
deliverables rather than chat replies, and proves that nothing left the building.

## Status

**M0 is underway, and there is a working checkpoint you can run today.** Foundations are
built and proven: `citadel_contracts`, the structural test suite, Postgres migrations, the
hash-chained audit chain, session identity with seeded demo users, and the policy
evaluator. `services/api` and `web/` wire all of that into a real HTTP API and a browser UI
— pulled forward of the rest of M0 on purpose, so there is something to run and look at
before the remaining pipeline work (the tool chokepoint, workers, retrieval) lands.

**What you can do right now**, after "Getting started" below:

- Sign in as one of three seeded demo identities (two engineers at different clearances and
  departments, one approver).
- Describe a resource and pick a tool. The real `registry/policy.yaml` rules decide
  allow/deny against your identity, and the UI shows which rule fired and why — this is the
  ACL demonstration ADR-0001 §Q7 calls for: same resource, different engineer, visibly
  different outcome.
- Watch every decision land in a real, hash-chained audit log, with a "verify chain" button.

Not built yet, on purpose and visibly so: the tool chokepoint and receipt issuance, the
`worker`/`sandbox` services, SSE task streaming, and retrieval. `AGENTS.md`'s "Current
state" section has the full, current detail — read it before changing anything.

- **Start here:** [`AGENTS.md`](./AGENTS.md)
- **Decisions:** [`docs/adr/`](./docs/adr/) — read 0001 and 0002 before writing code
- **Current milestone:** [`docs/PLAN-M0.md`](./docs/PLAN-M0.md)

## Getting started

```bash
cp .env.example .env
uv sync                     # first run needs network, to build the lockfile
scripts/dev-db.sh start     # a disposable local Postgres -- prints PGHOST/PGPORT/PGUSER;
                             # export them (or eval this line) before the next one
scripts/check.sh            # pytest + mypy --strict + ruff -- "is the repo green"
```

That proves the foundations. To run the checkpoint itself:

```bash
scripts/run-api.sh          # migrates the persistent demo database, then starts the API
```

Then open `http://127.0.0.1:8000` in a browser — the UI is served from the same process,
same origin, so there is nothing else to start or configure.

`uv sync` builds all nine workspace packages with `uv_build`, which ships with uv — so
once the lockfile and cache exist, `uv sync --offline` works with no network at all. That
is deliberate: the handoff requires no internet at **build** time, not just at run time.

**`scripts/*.sh` need a bash shell.** The target machine (ADR-0005) is a dedicated WSL2
distro with Docker Engine installed natively — do the above from inside it. On plain
Windows with no bash available, `uv run pytest`, `uv run mypy --strict <dirs>` and
`uv run ruff check .` still work directly against the packages, and every Postgres-backed
test skips cleanly (not an error) when no Postgres client tools are on `PATH` — but
`scripts/dev-db.sh` and `scripts/run-api.sh` are themselves bash, so actually running the
checkpoint needs WSL2 (or another real bash) rather than a plain Windows shell.

There is no `docker compose up` yet. It is task 11 of `docs/PLAN-M0.md`.

## Shape

A modular monolith plus isolated executors. The demonstration runs on **one box**
(ADR-0004): the RTX 5060 workstation runs Ollama, Postgres, the API, workers, the sandbox
and everything else. A two-box split — a separate GPU box running only the inference
runtime — is preserved as a documented deployment option, not the default and not
exercised at the demonstration. Module boundaries are enforced by structural tests, so
splitting into separate services or machines later is a deployment change, not a rewrite.

```
packages/    contracts platform gateway knowledge memory tools runtime deliverables sovereignty
services/    api worker sandbox
registry/    models, tools, policy, events, templates — data, never code
web/         workbench UI
tests/       structural (boundary enforcement) · unit · integration
eval/        evaluation harness (M2)
ops/         nftables, compose, offline bundle
```

## Profiles

| | `demo-local` | `hpc-eval` |
|---|---|---|
| Role | **The demonstration.** Sovereign. | **A measuring instrument.** Never sovereign. |
| Hardware | RTX 5060, 8 GB, single box | University HPC, DGX-H200 via SLURM |
| Runtime | Ollama | vLLM |
| Corpus | Real | Synthetic only — enforced at ingest |

**Design to `demo-local`, and demonstrate on it too.** The problem statement asks for a
single workstation with a mid-range GPU, so that is the product. `hpc-eval` benchmarks
models and runs the eval harness; it is never on the demonstration path, and acceptance
target E is never shown there (ADR-0003). A change that works on only one profile is not
done.

## Related repositories

- `AI_WORKBENCH/MONARCH` — memory, consumed as a version-pinned package. Two repositories
  with a dependency relationship, never a code merge.
- `AI_WORKBENCH/CITADEL` — the prototype. A quarry, not a baseline. `contracts/` is worth
  porting; most of the rest is documented in `AGENTS.md` as failure modes with names.
