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
prototype per `packages/contracts/AGENTS.md`, and verified. The structural test suite
(`docs/PLAN-M0.md` task, `tests/structural/AGENTS.md`) is also written: all ten detectors
it owes, each with a negative control and a not-vacuous check. `citadel_platform`'s
registry loader (`docs/PLAN-M0.md` task 4) is written: every `registry/*.yaml` file loads
under strict pydantic validation (unknown field, unknown classification, duplicate id, a
`fallback`/policy-operator typo -- all fail loudly, naming the file and, for a top-level
entry, the source line), for both profiles. Building it found two more registry bugs of
the same shape as the `classification_ceiling: restricted` one already fixed in
`profiles.yaml`: `models.demo-local.yaml` and `tools.yaml` both used `restricted`, which
`citadel_contracts.classification.Classification` has never defined -- fixed in those
files' own header comments. Postgres migrations and the audit chain (the rest of task 19,
`docs/PLAN-M0.md` tasks 5 and 6) are now built and empirically proven, closing task 19.
`packages/platform/migrations/0001`-`0004` cover the audit chain, identity/tasks/journal,
artifacts/approvals/registry-state, and pgvector document chunks; `citadel_platform.
migrations` applies and reverts them via a `psql` subprocess runner (no Postgres Python
driver needed, so it runs for real in this sandbox). The audit chain is
`citadel_platform.audit.chain` (pure-Python hashing and `verify()`, unit-tested with an
in-memory fake and a negative control for every way a chain can be broken) plus
`citadel_platform.audit.postgres` (the real psycopg writer/reader — type-checked here,
executable once a driver exists on the target machine). What actually delivers "one
logical writer" is migration 0001's `BEFORE INSERT` trigger, proven against a real
Postgres via `psql`/`subprocess` in `packages/platform/tests/test_audit_chain_schema.py`:
20 concurrent writers produce a gapless, unbroken chain; the app role cannot `UPDATE`/
`DELETE`; `verify()` catches a row edited after the anti-tamper trigger is disabled. That
proof caught a real bug on the way: `seq` as `GENERATED ALWAYS AS IDENTITY` is assigned
*before* a trigger runs and sequences do not block on each other, so two concurrent
writers could commit in the opposite order from the one they grabbed a `seq` value in —
confirmed by two genuine broken links under 10-way concurrency. Fixed by dropping
`IDENTITY` and having the trigger assign `seq` itself, from the same locked read that
determines `prev_hash`. `test_audit_chain_postgres.py` (the psycopg-driven counterpart)
and `test_vector_iterative_scan.py` (pgvector's `hnsw.iterative_scan`) are real,
reviewed, currently-collected-and-honestly-skipped code, gated on a driver and an
extension this sandbox cannot install — not deleted, not faked; they run on the WSL2
machine. `scripts/dev-db.sh` (below) is the prerequisite all of this package's Postgres
tests share.

Identity, roles, clearances and ACLs (`docs/PLAN-M0.md` task 7, the first sub-scope of
task 20) is now built and empirically proven. `citadel_contracts.identity` signs and
verifies session tokens — Ed25519 via PyJWT, shaped after `receipts.py` on purpose and
deliberately independent from it (distinct `TOKEN_TYPE`, distinct `SIGNING_ALGORITHM`
constant, never a shared import — `packages/contracts/AGENTS.md`'s new section), with
`citadel_contracts.domain.User` itself as the output type rather than a parallel
identity shape. `citadel_platform.identity.keys` owns the key lifecycle `identity.py`
deliberately does not: read `CITADEL_SESSION_SIGNING_KEY`, or fail loud-but-not-closed
with a fresh random per-process key, on the reasoning in that module's own docstring.
Migrations 0005 (a singular `role` column, `CHECK`-constrained to
`engineer`/`approver`/`admin` — singular by design, since `registry/policy.yaml`
branches on a singular `actor.role` even though `User.roles` is a tuple) and 0006 (the
three ADR-0001 §Q7 demo identities: two engineers at different clearances in different
departments, one approver) seed the Postgres side. `test_identity_seed.py` proves the
two engineers actually differ in both clearance and department rather than trusting the
`INSERT` by inspection, and proves 0006-then-0005's `down` migrations undo exactly what
they added. `test_identity_not_from_body` (already in the structural suite) continues to
pass — task 7's other "Done" clause.

Smoke-testing the new `python -m citadel_platform.identity genkey` CLI surfaced a real,
previously-unverified bug that predates this task: no package anywhere in the repo had a
`__main__.py`, so `python -m citadel_platform.<pkg>` — the exact invocation
`migrations/cli.py`'s own docstring and `argparse` `prog=` document — has never actually
worked, for `migrations` either, since task 5. Only `python -m
citadel_platform.migrations.cli` or an in-process `main()` call ever ran. Fixed for both
packages with a two-line `__main__.py` delegating to `cli.py`'s `main()`;
`packages/platform/AGENTS.md` now carries a standing note so a future CLI subpackage
does not repeat it. Neither `scripts/check.sh` nor any existing test caught this, because
pytest never shells out to a CLI via `-m` — confirmed fixed by re-running both
invocations for real (`genkey` against no database, `migrations status` against the dev
Postgres), not just by re-running the test suite.

Combined at that point: 197 tests pass, 2 skipped (101 contracts + 31 structural + 65
platform), `mypy --strict` clean across 60 source files, `ruff check` clean — all with the
disposable Postgres `scripts/dev-db.sh start` brings up, so the 2 skips are only the
psycopg/pgvector gaps above, never a missing database. Getting the migrations and audit
chain landed also fixed three real cross-package
tooling gaps that only show up once a second package has tests, two now and one earlier:
pytest's default import mode collided on two different `tests/` directories both resolving
to the bare module name `tests` (fixed by dropping `__init__.py` from every `tests/`
directory, repo-wide, and running pytest with `--import-mode=importlib` instead -- see the
comment on `addopts` in root `pyproject.toml`), mypy hit the identical collision under its
own module resolution (fixed by listing each test module individually in
`tool.mypy.overrides`, the same file), and mypy hit the same collision a third time over
two different `conftest.py` files (`tests/structural/` and a would-be
`packages/platform/tests/`) -- unfixable by another override, since this time it was two
*different* files both wanting to be the one module `conftest`, not one file needing
different settings. Fixed by not using a second `conftest.py` at all:
`packages/platform/tests/pg_scratch.py` is a plain, uniquely-named module, imported
explicitly by the tests that need it, with `packages/platform/tests` added to root
`pyproject.toml`'s `pythonpath` so it resolves. The other seven packages' `tests/__init__.py`
stubs had been left in place when the first fix landed -- inconsistent with the "no
`__init__.py` under any `tests/`" invariant it established, and a silent reintroduction of
the same collision waiting for whichever package's tests are written next -- found and
deleted while building `scripts/check.sh` below, which is what now runs across all nine
packages, not just the two with tests today.

The policy evaluator (`docs/PLAN-M0.md` task 8, first half) is now built and proven
against the real registry. `citadel_tools.policy.evaluate(rules, actor=, resource=,
tool=, receipt=) -> Decision` is a pure function — no I/O, no audit-chain write —
implementing `registry/policy.yaml`'s ordered, first-match-wins, default-deny semantics
over its five operators. `resource` and `tool` are `citadel_contracts.domain.Resource`
and `citadel_platform.registry.schema.ToolEntry` directly, since both already carry
exactly the fields the rules reference; `actor` needed one new shape, `ActorFacts`,
because `User` carries `roles`, not `capabilities` — closed by a new registry file,
`registry/roles.yaml` (role → capability grants: engineer/admin get the full working
set, approver deliberately gets only `retrieval`, so an approver cannot `fs.write`/
`code.run`/`doc.generate` even by mistake — capability-enforced, not just
convention-enforced), and by `actor_facts_from_user()`, the one function that turns a
`User` into an `ActorFacts` and does the two boundary normalisations that requires
(reject more than one role; uppercase `clearance`) rather than leaving them implicit.
`test_policy.py` has one test per real rule in `registry/policy.yaml`, in file order,
plus the default-deny case no rule in the file encodes — task 8's first two `Done`
clauses. Its third — every decision, allow and deny alike, produces an audit event —
waits on the chokepoint below, since the evaluator itself never writes anything.

Building it found two more real bugs, same shape as the `__main__.py` one above:
documented behaviour the code never actually implemented, caught by being the first
genuine consumer. First, in `citadel_platform`: `_known_classification()`
(`registry/schema.py`) validated a registry classification value via
`Classification.rank(value.upper())` but returned the original, un-uppercased `value` —
so every `classification_ceiling` read off the registry silently kept its as-written
lowercase YAML casing instead of becoming the lattice's uppercase form, contradicting
the function's own docstring. Nothing had ever called `Classification.rank()`/
`.exceeds()` on a registry-sourced value before the evaluator's `exceeds` operator did,
which is exactly why eight of the evaluator's own tests were the ones to catch it
(`ValueError: unknown classification 'confidential'`). Fixed by uppercasing before
returning, not only before validating — `packages/platform/AGENTS.md` has the rest.
Second, in the test infrastructure rather than in code: root `pyproject.toml`'s
per-module mypy override that exempts test files from `disallow_untyped_defs` (test
functions are `def test_x():`, never `def test_x() -> None:`) does not by itself cover
a *typed fixture parameter* (`def test_x(registry: Registry):` — governed by the
separate `disallow_incomplete_defs`) or a *parenthesized* `@pytest.fixture(
scope="module")` (governed by `disallow_untyped_decorators`) — both first used by
`test_policy.py`, for real reasons: a self-documenting fixture type, and a
module-scoped fixture so the real registry loads from YAML once per file, not once per
test. Fixed by adding both flags to the same override, with the reasoning recorded
there rather than only here.

Combined: 236 tests pass, 2 skipped (101 contracts + 31 structural + 68 platform + 36
tools), `mypy --strict` clean across 62 source files, `ruff check` clean — same
disposable-Postgres caveat as above; the 2 skips are still only the psycopg/pgvector
gaps, never a missing database. Everything else is still the skeleton this file, the
ADRs, the registries and the plan describe. **Resume at `docs/PLAN-M0.md` task 8's
second half** — the chokepoint itself, in the still-empty-of-it `citadel_tools` package
(`tests/structural/test_single_chokepoint.py`'s `TOOL_REGISTRY`/`execute_tool`/
`dispatch_tool` naming contract): tool resolution from the registry, JSON-schema
argument validation against a tool's declared schema, dispatch, and turning a
`Decision` into an audit event for both allow and deny. Task 7 was done already; the
evaluator half of task 8 is done as of this paragraph; task 20 was scoped as identity
plus ACLs plus the chokepoint together, and is now down to its last third.

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

M0 targets `demo-local` only: **one machine**, the RTX 5060 workstation, running
everything (ADR-0004). `hpc-eval` is M1 work and has no Compose file — it runs under
Apptainer on SLURM (`ops/hpc/README.md`).

**The demonstration machine is Windows with Docker Desktop and WSL2.** The whole stack
runs inside a dedicated WSL2 distro with Docker Engine installed natively — never inside
Docker Desktop's own managed WSL2 backend, whose network stack is not one you control. See
ADR-0005 and `ops/wsl2/README.md`. Verify GPU passthrough (`nvidia-smi` inside the distro)
before M0 task 12 — that check is called out as blocking in the ADR.
