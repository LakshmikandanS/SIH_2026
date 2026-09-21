# Citadel

A sovereign, air-gapped agentic AI workbench for confidential industrial work.

Refineries, PSUs, defence-linked manufacturers and government offices produce a great deal
of routine but sensitive knowledge work — approval notes, engineering calculations,
internal tooling, review of scanned drawings and inspection reports. None of it can go to a
cloud assistant. Citadel runs entirely on the organisation's own hardware, produces real
deliverables rather than chat replies, and proves that nothing left the building.

## Status

**M0 has not started.** This repository is a skeleton: instructions, decisions, registries
and a plan.

- **Start here:** [`AGENTS.md`](./AGENTS.md)
- **Decisions:** [`docs/adr/`](./docs/adr/) — read 0001 and 0002 before writing code
- **Current milestone:** [`docs/PLAN-M0.md`](./docs/PLAN-M0.md)

## Getting started

```bash
cp .env.example .env
uv sync                 # first run needs network, to build the lockfile
uv run pytest -m structural
```

`uv sync` builds all nine workspace packages with `uv_build`, which ships with uv — so
once the lockfile and cache exist, `uv sync --offline` works with no network at all. That
is deliberate: the handoff requires no internet at **build** time, not just at run time.

There is no `docker compose up` yet. It is task 11 of `docs/PLAN-M0.md`.

## Shape

A modular monolith plus isolated executors, across two boxes: an application box that runs
everything CPU-bound, and a GPU box that runs the inference runtime and nothing else.
Module boundaries are enforced by structural tests, so splitting into separate services
later is a deployment change rather than a rewrite.

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
