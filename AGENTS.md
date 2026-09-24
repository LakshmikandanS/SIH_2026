# AGENTS.md — Citadel

Read this file completely before writing any code. Then read the ten ADRs in
`docs/adr/`:

- 0001: founding decisions
- 0002: hardware profiles and runtime
- 0003: what the HPC cluster is and is not for
- 0004: one box
- 0005: the WSL2 execution environment
- 0006: the Docker Desktop launcher and per-container enforcement
- 0007: the sandbox as a service
- 0008: a workbench where agents and people write reports together
- 0009: the memory manager, Monarch's design in Citadel's store
- 0010: "web search" is the offline reference library

Then read the `AGENTS.md` in the package you are about to touch. Do not skip to the code.

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
   tools              ← contracts, platform, gateway, knowledge, deliverables, memory
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
| `memory` | Working memory (task-scoped). Episodic and semantic memory kept by the memory manager: Monarch's pipeline, scoped like documents (ADR-0009) |
| `tools` | Tool plugins with declared schemas, the policy chokepoint, the tool registry |
| `runtime` | Planner, agent loop, replanner, step journal, budgets, cancellation, worker pool; a lead with helper agents, shared state, and people working on the same task (ADR-0008) |
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
  Propose the tag, the quantisation and the VRAM cost; wait. The four entries with
  `enabled: true` in `registry/models.demo-local.yaml` are approved, and that file's
  header records when. `reason-large` is not approved. Enabling is not pulling: a person
  starts every download (`citadel models`, or *Pull missing models* in the UI).
- Adding a runtime dependency that is not already in a lockfile — everything must be
  installable offline.
- Changing anything in `contracts/` after M0, since it is the shared vocabulary.
- Anything that weakens an invariant above, including "temporarily".
- Any design decision that an ADR does not cover and that will be expensive to reverse.

---

## Current state

**The five acceptance targets run end to end** in demonstration form. `README.md` walks
through each of them, and `citadel.cmd` starts the whole stack on Windows with one command
(Docker Desktop plus Ollama for Windows, [ADR-0006](./docs/adr/0006-docker-desktop-launcher-and-per-container-enforcement.md)).
Getting there pulled a good deal of M1–M5 forward of PLAN-M0's milestone order, at Fahim's
explicit request ("complete the project"). The order below is the dependency order, not
the order the milestones planned.

**Report writing in a workbench** ([ADR-0008](./docs/adr/0008-a-workbench-where-agents-and-people-write-reports-together.md)),
at Fahim's request: "the main task to focus is report writing: agent can give report
accordingly to user prompt, user can modify if he wants."

- **Shaped by the prompt.** `/report on lathe L-1 covering only its condition and the vendor
  options` produces a `report` with exactly those two sections. Every paragraph is cited,
  and every version is rendered and verified.
- **Edited by hand, or revised by the agents.** The person edits any section in the
  report tab and saves it as a new verified version, or sends it back with an
  instruction (**Revise**). The agents then start from the newest verified version, hand
  edits included.
- **The room around it** is the IDE the vision image drew:
  - a resource tree, tabs and a command line;
  - agent cards and helper agents sharing one task state;
  - people pausing, steering, adding notes and running tools through the same chokepoint;
  - observability, sandbox-state and activity panels;
  - the memory manager ([ADR-0009](./docs/adr/0009-the-memory-manager-monarchs-design-in-citadels-store.md)).

**Foundations** (M0 tasks 1–9, 13): `citadel_contracts`, the structural suite (every
detector with a negative control), strict registry loading, migrations `0001`–`0010`, the
hash-chained audit log, identity and the seeded demo cast, the policy evaluator, tracing
and metrics. Their history, including the bugs each step caught, is in
`docs/history/m0-build-notes.md`.

**What each package now does:**

| Package | Built |
|---|---|
| `gateway` | Ollama and vLLM providers; deterministic routing with a retained per-candidate score breakdown; fallback that says so; structured output with honest retries; GPU admission; residency and warm-up; pulls a human starts. Its HTTP clients never honour proxy variables. |
| `knowledge` | Ingestion (detect → PDF text / OCR → vision reading of scanned pages → chunk → embed) behind CPU admission. Retrieval filters by ACL and classification in the same SQL statement as the vector search, then does reciprocal-rank fusion with lexical search and a CPU rerank, and counts denials. Evidence ids: E# for document regions, C# for computations. |
| `tools` | The chokepoint (`Chokepoint.invoke`): resolve → validate the JSON schema → evaluate policy → audit allow and deny → sign a receipt bound to the resource digest → dispatch. Nothing is released unless the executing boundary verified the receipt. The fifteen tools in `registry/tools.yaml` are plugins discovered from their manifest, `web.search` among them as the offline reference library ([ADR-0010](./docs/adr/0010-web-search-is-the-offline-reference-library.md)). People run them through the same function. `sandbox.run_verified` is the code boundary. |
| `deliverables` | docx and xlsx from the four templates in `registry/templates/`, the `report` among them, whose sections follow the request; the four-tier verification ladder, driven by each template's declaration and applied per paragraph for a report; every edit a new verified version; approval with separation of duties where a template asks for it; re-render, hash and release with a provenance record. |
| `runtime` | Planner (free-form plans against the real goal), agent loop, a journal with a task resumable from it, budgets checked each iteration, cancellation, pause and resume, helper agents in dependency waves with a shared state, people's steps journalled as `human:<id>`, one bounded revision after a rejection and up to five at the owner's request, `/ask` tasks, and a worker pool claiming with `SKIP LOCKED`. |
| `memory` | Working memory, task-scoped. Episodic and semantic memory in Postgres, kept by the memory manager: extract → related (same compartment and tier) → a model proposes one of six operations → a deterministic executor, with `create` as the lossless fallback. Filtered in SQL like documents; grounding, never evidence. |
| `sovereignty` | Telemetry (an audit-hook monitor and a psutil connection scanner, both attributed to task and agent), the in-process fence, the deliberate probe, and the panel and per-task reports. Enforcement is `ops/compose/egress-entrypoint.sh` plus `ops/nftables/citadel-egress.nft`, applied per container. |

**Services** (`services/AGENTS.md`):

- `api`: HTTP, SSE task streams, the workbench's endpoints (reports, edits, revisions,
  notes, drafts, activity, panels, memory) and the web UI.
- `worker`: task threads, ingestion, and seeding the demo corpus once the approved models
  are installed.
- Both write a heartbeat row every few seconds (`service_heartbeats`), which the container
  health panel reads.
- `sandbox`: one hardened service on an internal network, not a container per run,
  [ADR-0007](./docs/adr/0007-the-sandbox-is-a-service-not-a-container-per-run.md).

**How it runs:**

| Path | Command |
|---|---|
| Windows | `citadel.cmd` |
| Linux or WSL2 (the ADR-0005 configuration) | `scripts/up.sh` |
| No containers | `scripts/run.sh [--fake-models]` |

**The Compose stack has not yet run on a real Docker daemon.** The environment it was built
in cannot start one. There, the stack's process model ran in network namespaces:

- the image's `PYTHONPATH` and a clean environment;
- api and worker through the real entrypoint, with exactly the Compose capabilities, as
  uid 10001 with no capabilities left and `no_new_privs`;
- the sandbox as uid 10002;
- the ruleset applied, including the inbound refusal of the sandbox network;
- every UI flow walked end to end, approval included.

That run found three problems, all now fixed:

- the missing `SETPCAP`;
- the ambient-proxy exposure;
- a connection scanner that would have counted the operator's own browser as egress,
  because Docker delivers a published port's connections from outside every trusted
  network. That run used a browser at such an address, and the panel read zero.

What is left to learn on the demonstration machine is whether Docker Desktop's kernel
accepts the ruleset in a container namespace. The panel says so either way.

**Two substitutions from the build sandbox still stand**: Starlette + uvicorn for FastAPI,
and hand-written HTML/CSS/JS for Vite + React (`services/AGENTS.md`, `web/AGENTS.md`).

**`scripts/check.sh` is green**: 396 tests pass and 1 is skipped (the psycopg audit
writer, which has no driver in the build sandbox). `mypy --strict` is clean across 157
files, and so is ruff. The workbench has also been walked in a browser (Playwright) against
`scripts/run.sh --fake-models`: a report from a prompt, a hand edit, a revision by the
agents, an uncited number flagged, `/ask`, and every panel, with no page errors.

**Not done, and where to resume:**

1. **The four M1 measurements** (`docs/PLAN-M0.md`, end), on the real card: resident-set
   VRAM, swap cost, and structured-output conformance per model. Every `vram_gb` and
   `swap_cost_s` in `registry/models.demo-local.yaml` is still an estimate.
2. **The offline install drill** (PLAN-M0 task 14).
3. **The two-box Compose override**, and the host-level ruleset for a WSL2 distro
   (ADR-0006, *Revisit when*).
4. **PowerPoint deliverables.**
5. **Real sign-in.** Demonstration identities have no password.
6. **The `hpc-eval` profile**, which loads and routes but has never run on the cluster.
7. **The report editor on real models.** Report writing, editing and revision have been
   walked end to end with the scripted stand-in model, in a browser. Whether `qwen3:4b`
   follows the section instructions as well is an M1 measurement, like structured-output
   conformance.

### Verifying the repo: `scripts/check.sh`

`scripts/check.sh` is pytest + `mypy --strict` + ruff, in that order, run the same way
whether this is a network-restricted dev sandbox or the real WSL2 machine — it is what
this file's "Current state" section reports the pass/fail of, and it is what "the repo
works" means in this project. Run it before calling anything done:

```
scripts/check.sh
```

All three stages always run, even if an earlier one fails, so one pass reports everything
broken rather than a fix-and-rerun cycle per stage; exit code is 0 only if all three
passed. `scripts/test.sh [pytest args...]` is the pytest-only stage alone, for a fast
edit-run loop — it is not a substitute for `scripts/check.sh` before calling something
done. Neither script takes a path to `uv`, a profile name, or a flag for which kind of
machine this is: `scripts/lib/env.sh` decides that itself, once per run, by actually trying
`uv run --no-sync` (works once `uv sync` has populated `.venv` — the real WSL2 machine
after setup) and falling back to pre-installed global tools plus a `PYTHONPATH` bridge
(this kind of sandbox, detailed in that file's own header) when it doesn't. Add a new
package or a stub package's first real test file and both scripts pick it up on their own
— nothing here needs updating for that.

If `scripts/check.sh` reports no usable toolchain, that's the sandbox-vs-machine
distinction it depends on actually failing on this box: either run `uv sync` at the repo
root (needs package-registry access), or install pytest, mypy and ruff globally. This is a
fact about *this kind of restricted sandbox*, not about the target demonstration machine —
the WSL2 box (ADR-0005) has ordinary internet access and `uv sync` there needs none of it.
Don't let the sandbox branch in `scripts/lib/env.sh` leak into `ops/` or the Compose files.

`packages/platform`'s Postgres-backed tests need a reachable server first:
`scripts/dev-db.sh start` brings up a disposable, socket-only local one (never
`ops/compose/`'s containerized Postgres — see that script's own header) and prints the
`PGHOST`/`PGPORT`/`PGUSER` to export before running `scripts/check.sh` or
`scripts/test.sh`. Without them, those tests skip cleanly instead of failing red.
`scripts/dev-db.sh stop` tears it down; `status` reports whether it's up. Same
root-vs-non-root, try-then-fall-back detection as `scripts/lib/env.sh`, so it runs
unchanged on this sandbox and on the WSL2 machine.

The demonstration targets `demo-local`: **one machine**, the RTX 5060 workstation,
running everything (ADR-0004). `hpc-eval` has no Compose file; it runs under Apptainer on
SLURM (`ops/hpc/README.md`).

**The demonstration machine is Windows**, and there are two ways to run the one Compose
file on it:

- **`citadel.cmd`, on Docker Desktop, with Ollama for Windows on the host.** This is the
  one-command path ([ADR-0006](./docs/adr/0006-docker-desktop-launcher-and-per-container-enforcement.md)).
  The egress ruleset lives in each container's own network namespace, so it does not
  depend on which Docker runs it. The host, and Ollama on it, sit outside the rules; the
  sovereignty panel says so, and the cable test covers them.
- **A dedicated WSL2 distro with Docker Engine and Ollama inside it** (ADR-0005,
  `ops/wsl2/README.md`, `scripts/up.sh`). This is the stronger configuration once the
  distro-level ruleset exists. Verify GPU passthrough there (`nvidia-smi` inside the
  distro) before relying on it.
