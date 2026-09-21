# AGENTS.md — Citadel

Read this file completely before writing any code. Then read the five ADRs in
`docs/adr/` — 0001 founding decisions, 0002 hardware profiles and runtime, 0003 what the
HPC cluster is and is not for, 0004 one box, 0005 the WSL2 execution environment. Then read
the `AGENTS.md` in the package you are about to touch. Do not skip to the code.

---

## What this is

**Citadel** is a sovereign, air-gapped agentic AI workbench for confidential industrial
work — refineries, PSUs, defence-linked manufacturers, government offices. It runs entirely
on the organisation's own hardware. No request leaves the premises, and the system proves
it rather than claiming it.

The output of a task is a **deliverable** — a Word approval note, an Excel calculation, a
verified program — not a chat reply. That framing is the product. If you find yourself
building a chatbot with extra steps, stop.

### The five acceptance targets

Every line of code exists to serve one of these. Design backwards from them.

| | Must demonstrate |
|---|---|
| **A** | Model auto-selection across at least two distinct task types, with the reason visible |
| **B** | Scanned inspection report → extract findings → draft an approval note as a **.docx** |
| **C** | A coding task written, executed and verified in a sandbox |
| **D** | A multimodal task: image or scanned-document understanding |
| **E** | **Zero external calls**, shown live through logs or a network monitor |

---

## Non-negotiable invariants

Each of these is enforced by a test in `tests/structural/`. Each of those tests ships with
a **negative control** — a fixture that the detector must catch — so the test cannot
silently become vacuous. If you add an invariant, you add both.

1. **No model identifier, VRAM figure, parameter count or quantisation level appears
   outside `registry/`.** Adding a model is a registry entry plus a deploy. Never a code
   change.
2. **Only `packages/gateway` may import an inference client or reference an inference
   host.** Everything else asks the gateway.
3. **Identity comes from a verified session token, never from a request body.** The discard
   of any caller-supplied identity field is visible in the code, not implied.
4. **Classification is a lattice with explicit comparison. Never string ordering.** An
   unknown marking is not comparable, and therefore denied.
5. **Authorization filters before ranking.** A record the caller may not see is never
   constructed into a result — not filtered out afterwards. In Postgres this is a `WHERE`
   clause in the same query as the vector search.
6. **Every tool invocation passes through one chokepoint function.** No exceptions, no
   direct calls, no "just this once" path.
7. **Policy is data.** A rule table, first match wins, default deny. A new tool or
   department must never require a code change.
8. **The audit chain is append-only, hash-chained, one logical writer.** It is never
   sampled and never truncated.
9. **The module dependency rule below holds.** No cycles, no reaching past a layer.
10. **No egress.** No code makes an outbound network call. The frontend build embeds no
    external URL — no CDN, no font host, no icon service.
11. **Confidential material never reaches `hpc-eval`.** The university HPC is shared,
    third-party-administered infrastructure. Its profile carries
    `classification_ceiling: public`, enforced at ingest by the same lattice comparison
    that guards tool invocation — so that deployment *cannot* hold restricted material
    rather than merely being asked not to. See ADR-0003.

---

## Module map and the dependency rule

```
services/{api,worker,sandbox}   ← may import anything
        │
   runtime            ← contracts, platform, gateway, tools, memory
        │
   tools              ← contracts, platform, gateway, knowledge, deliverables
        │
 knowledge   memory          ← contracts, platform, gateway
 deliverables  sovereignty     ← contracts, platform
 gateway                       ← contracts, platform, + an inference client
        │
   platform           ← contracts
        │
   contracts          ← NOTHING. Zero inward dependencies.
```

Each package's `pyproject.toml` declares only the dependencies this rule permits. That
declaration is documentation and it catches a violation when a package is built alone — but
**it does not stop a forbidden import inside a synced workspace**, because every member is
installed into the same environment. `tests/structural/test_module_boundaries.py` is what
actually enforces the rule. Do not trust the manifest and skip the test.

**Forbidden edges, named because they are the tempting ones:**

- `runtime ✗ knowledge` — retrieval is reached through the `docs.*` tools, so that it
  passes the policy chokepoint like everything else.
- `runtime ✗ deliverables` — document generation is the `doc.generate` tool, same reason.
- `contracts ✗ anything` — it is portable precisely because it depends on nothing. The
  prototype enforced this with a test and that is why it ports cleanly. Keep the test.
- Nothing imports `services`.

| Package | Owns |
|---|---|
| `contracts` | Domain types, envelopes, event registry, classification lattice, state machines, signed decision receipts |
| `platform` | Postgres access, migrations, audit chain writer, config and registry loading, tracing, metrics |
| `gateway` | Model registry, health probing, routing with retained score breakdown, residency, the `InferenceProvider` implementations |
| `knowledge` | Ingestion (detect → OCR/vision → normalise → chunk → embed → index), hybrid retrieval, reranking, citation assembly |
| `memory` | Working memory (task-scoped, Citadel-side). Episodic and semantic behind the Monarch seam |
| `tools` | Tool plugins with declared schemas, the policy chokepoint, the tool registry |
| `runtime` | Planner, agent loop, replanner, step journal, budgets, cancellation, worker pool |
| `deliverables` | Template registry, docx/xlsx/pptx generators, the four-tier verification ladder, release and hashing |
| `sovereignty` | Egress telemetry, per-task sovereignty report, the deliberate probe |

---

## Failure modes with names

These are not hypothetical. Each one is a specific thing that happened in the prototype at
`AI_WORKBENCH/CITADEL`, and collectively they are why this is a new repository. If you
catch yourself doing any of them, you are rebuilding the thing we walked away from.

- **The hardcoded scenario.** The prototype held the demo task as a string constant,
  ignored the user's actual goal when planning, instructed the model to emit a fixed
  three-step plan, then *validated that the plan had exactly that shape*. Plans have no
  fixed shape or length. The planner sees the real goal text.
- **The `if/elif` observation handler.** Keyed on three tool names, so adding a tool meant
  editing the agent loop. Observation handling dispatches on the tool's declared schema.
- **Verification pinned to one document type.** The prototype's verifier checked for the
  literal headings `## Summary`, `## Maintenance History`, `## Sources`. Verification is
  driven by the template's declared structure, never by hardcoded titles.
- **The closed vocabulary.** Sixteen event types, and anything new was rejected. Every
  vocabulary in this repo — events, memory types, tool names, classifications — is an
  open registry. A closed `StrEnum` that rejects a new member is this bug.
- **`UNIQUE(task_id)` on agents.** "One agent per task" enforced by a schema constraint.
  Do not encode scope cuts as invariants.
- **Synchronous in-request execution.** The whole agentic task ran inside one HTTP request.
  Tasks go to a queue, workers pull them, the journal makes them resumable, SSE streams
  progress, and cancellation works.
- **`fallback_chain = []` and no health checking, as deliberate design.** Reasonable for a
  slice, unacceptable here.
- **Empty structured output, silently substituted.** The prototype hit `qwen3:4B` returning
  nothing under `format=json` and quietly swapped in a different model behind the same
  logical id. If a model is substituted, the substitution is recorded and surfaced.
  **Degrade honestly.**

The general shape of all of these: *a scope cut hardened into an invariant, then a test
written to enforce it.* When you cut scope — and you will — cut it so the cut is a TODO
and a registry entry, never an assertion.

---

## How to work

1. **Milestones are ordered and each ends in a demonstration.** Do not start M(n+1) until
   M(n)'s demonstration actually runs. `docs/PLAN-M0.md` is the current one.
2. **Structural tests from day one**, not after. They are cheap now and they are the only
   thing that keeps the boundaries real.
3. **Registries are data.** If your change adds a branch on a model name, a tool name, a
   department or a document type, it belongs in a registry file instead.
4. **Both profiles.** `demo-local` and `hpc-eval` (ADR-0002, ADR-0003). Code that works on
   only one is not done. But they are not peers: `demo-local` is the product and carries
   the demonstration, `hpc-eval` is a measuring instrument that never sees confidential
   material and is never on the demonstration path.
5. **Small, reviewable commits.** One decision per commit. Reference the ADR when a commit
   implements one.
6. **When you disagree with an ADR, write one.** Do not quietly implement something else.
   A superseding ADR is cheap; a codebase that contradicts its own documentation is not.

### Conventions

- Python 3.12+, `uv` for dependency management, one `pyproject.toml` per package.
- Type hints everywhere. `from __future__ import annotations` at the top of every module.
- `pytest`, with `--strict-markers`. Markers: `unit`, `integration`, `structural`.
- No package may be imported by its filesystem path. Everything is an installed package,
  so the boundaries are real at import time and not just on a diagram.
- Docstrings say *why*, especially at a boundary. The prototype's `app/rag/search.py`
  header is the standard to match: it explains what the module refuses to do and why that
  refusal is structural.

---

## Ask a human before

- **Pulling any model.** Fahim has asked to approve model downloads by name and size.
  Propose the tag, the quantisation and the VRAM cost; wait.
- Adding a runtime dependency that is not already in a lockfile — everything must be
  installable offline.
- Changing anything in `contracts/` after M0, since it is the shared vocabulary.
- Anything that weakens an invariant above, including "temporarily".
- Any design decision that an ADR does not cover and that will be expensive to reverse.

---

## Current state

M0 is underway. `citadel_contracts` (`docs/PLAN-M0.md` task 3) is written, ported from the
prototype per `packages/contracts/AGENTS.md`, and verified: 88 tests pass, `mypy --strict`
and `ruff check` are both clean. Everything else is still the skeleton this file, the ADRs,
the registries and the plan describe. **Resume at `docs/PLAN-M0.md`, the next unchecked
task** (structural test suite, then platform).

### Running tests/mypy/ruff in a network-restricted dev sandbox

If you are working in a cloud dev sandbox where `uv sync` fails resolving the `dev`
dependency group (`mypy was not found in the package registry... 403 Forbidden`) — that is
the sandbox's PyPI egress being blocked, not a real dependency problem. Check for
pre-installed global tools before concluding a package is unavailable:

```
pytest --version   # and mypy, ruff, black, poetry, pyright -- check `uv tool list`
```

If they're there (they were, in the sandbox this repo was first built in — `uv tool
install`, isolated from the workspace venv), they still can't see the workspace's own
packages or its `pyjwt`/`cryptography`. Point `PYTHONPATH` at both the package's `src/` and
wherever the sandbox's base Python keeps its pre-baked packages (found with
`python3 -c "import jwt; print(jwt.__file__)"`), e.g.:

```
PYTHONPATH="<jwt/cryptography's dist-packages dir>:packages/contracts/src" \
  pytest packages/contracts/tests -q
```

This is a fact about *this kind of restricted sandbox*, not about the target demonstration
machine — the WSL2 box (ADR-0005) has ordinary internet access and `uv sync` there needs
none of this. Don't let a sandbox workaround leak into `ops/` or the Compose files.

M0 targets `demo-local` only: **one machine**, the RTX 5060 workstation, running
everything (ADR-0004). `hpc-eval` is M1 work and has no Compose file — it runs under
Apptainer on SLURM (`ops/hpc/README.md`).

**The demonstration machine is Windows with Docker Desktop and WSL2.** The whole stack
runs inside a dedicated WSL2 distro with Docker Engine installed natively — never inside
Docker Desktop's own managed WSL2 backend, whose network stack is not one you control. See
ADR-0005 and `ops/wsl2/README.md`. Verify GPU passthrough (`nvidia-smi` inside the distro)
before M0 task 12 — that check is called out as blocking in the ADR.
