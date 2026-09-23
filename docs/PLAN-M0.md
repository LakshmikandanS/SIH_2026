# M0 — Foundations

**Goal.** A running, empty system with real boundaries. No AI capability yet, on purpose:
M0 exists so that every later milestone lands on something that already enforces its own
rules.

**Done when — all three, demonstrated, not asserted:**

1. `docker compose up` brings up a running system on the `demo-local` profile.
2. The audit chain writes, and `verify()` recomputes it end to end.
3. The structural test suite passes, **and each detector catches its negative control.**

Read `AGENTS.md`, then all five ADRs in `docs/adr/`, then the `AGENTS.md`
of the package you are touching. Tasks are ordered; each says what "done" means.

---

## 1. Workspace and tooling

`uv` workspace at the root. One `pyproject.toml` per package under `packages/`, each
declaring **only** the internal dependencies the layer rule permits. Python 3.12+.

**This already works — it was built and verified before this plan was written.** All nine
packages resolve, build and import with no network at all.

Two things about it are deliberate:

- **Build backend is `uv_build`, not setuptools.** setuptools has to be fetched from PyPI
  for every package build, which breaks the offline-build requirement (handoff §3.2).
  `uv_build` ships with uv, so the workspace builds with the network off. This was found
  by trying it: with setuptools the offline build fails on the build backend alone.
- **The manifests declare the layer rule but do not enforce it.** Inside a synced
  workspace every member is installed into the same environment, so `citadel_runtime` can
  still import `citadel_knowledge` at runtime. Verified. The manifest catches it only when
  a package is built alone. **`tests/structural` is the real enforcement** — which is why
  task 2 comes before any real code.

**Done:** `uv sync` installs all nine packages; `uv sync --offline` works once the lockfile
and cache exist. (Locking itself needs network once, for the dev tools.)

## 2. Structural test harness — before any real code

Build the harness and the negative-control mechanism first. Yes, first.

A detector written after the code it polices gets written to pass. Written before, it
shapes what gets written.

Start with two detectors so the pattern is established:
`test_contracts_is_self_contained` and `test_module_boundaries`.

**Done:** both detectors pass on the empty tree **and** fail on their control fixtures.
A deliberately broken control makes the suite red.

## 3. Port `contracts`

From `AI_WORKBENCH/CITADEL/contracts/`. See `packages/contracts/AGENTS.md` for what
ports as-is and what changes on the way in.

Two changes are mandatory and are not optional cleanups:
- **The event vocabulary becomes open** — loaded from `registry/events.yaml`, registration
  rather than a closed `StrEnum`.
- **Signing key material gets a documented lifecycle.** A per-process random secret stays
  as the fail-closed default; it stops being the only option.

Bring `tests/test_receipts.py` across in full. It is adversarial and it is the reason to
trust the receipts.

**Done:** `contracts` tests pass; `test_contracts_is_self_contained` passes; the
classification lattice denies an unknown marking; a tampered receipt is rejected.

## 4. Registry loading in `platform`

Load and validate `registry/*.yaml` under `CITADEL_PROFILE`. **Strict validation** — an
unknown field is an error. Typed objects out.

**Done:** both profiles load; a deliberately misspelled field fails loudly with the file
and line; a missing required field names what is missing.

## 5. Postgres, migrations, `pgvector`

Schema for tasks, journals, users, artifacts, approvals, the audit chain and the
registries' runtime state. Migrations from the first commit — never a hand-edited schema.

**Enable `hnsw.iterative_scan`.** Write the test now even though there is nothing to
retrieve yet: a filtered vector query that excludes most of the corpus must still return
`k` rows. It will matter at M2 and it is invisible until it bites.

**Done:** migrations run forward and back; the iterative-scan test passes.

## 6. The audit chain

Append-only, hash-chained, **one logical writer** — serialised inside the database, not by
a mutex. `verify()` recomputes the whole chain and is exposed for the UI.

**Done:** concurrent writers from multiple processes produce an unbroken chain; `verify()`
detects a row tampered with directly in the database.

## 7. Identity, roles, clearances, ACLs

ADR-0001 §Q5 moves this to M0 because the demonstration shows ACL filtering (§Q7).

Identity from a **verified session token**. The discard of any caller-supplied identity
field is **visible in the code**.

Seed three identities for the demonstration: two engineers at different classification
levels in different departments, and one approver.

**Done:** `test_identity_not_from_body` passes with its control; the three seeded
identities differ in clearance and department.

## 8. Policy evaluator

Rules from `registry/policy.yaml`. Ordered, first match wins, **default deny**.

The default lives in the evaluator, not as a final rule in the file — a default that can be
deleted is not a default. Test it directly.

**Done:** each rule has a test; an empty rule file denies everything; every decision,
allow and deny alike, produces an audit event.

## 9. Tracing and metrics

Spans and time series, **kept separate from the audit chain** (`packages/platform/AGENTS.md`).
Span tree per task, even though tasks are trivial at M0.

**Done:** a request produces a span tree; metrics expose latency and error rate; nothing
writes the same payload to both the audit chain and the trace.

## 10. Service skeletons

`api` (FastAPI, auth, health, SSE endpoint that streams nothing yet), `worker` (pulls an
empty queue), `sandbox` (image builds, `--network none`, caps enforced, destroyed after
use).

Thin. No logic here — see `services/AGENTS.md`.

**`api` is built, ahead of schedule, as the M0 checkpoint** (root AGENTS.md's "Current
state", `services/AGENTS.md`): real `health`, auth (login for a seeded demo identity +
verified-session-token `/api/me`), and the ACL policy demonstration wired to the real
evaluator and audit chain, with a real web UI (`web/`) over it — but as Starlette, not
FastAPI (sandbox substitution, see those files), and with no SSE endpoint yet, because
there is no task system yet for one to stream. `worker` and `sandbox` are not started.

**Done:** all three start; the sandbox image runs a trivial program and is destroyed;
a network call from inside it fails.

## 11. Compose for both boxes

`docker-compose.yml` for the app box, plus an override for the GPU box. One command up,
one command down.

**Not `hpc-eval`.** That profile runs under Apptainer on SLURM and has no Compose file —
see `ops/hpc/README.md`. M0 is `demo-local` only.

The Model Gateway holds the inference endpoint in the registry, so one box and two boxes
differ by a URL. Prove that now, while it is cheap.

Target **one box** (ADR-0004): Ollama, Postgres, API, worker, sandbox and the CPU-side
work all on the RTX 5060 machine. The two-box split becomes `ops/compose/two-box.yml`,
written and documented but not exercised.

**Done:** `docker compose up` works; switching `CITADEL_PROFILE` changes the endpoint and
nothing else; collapsing to one box needs no code change.

## 12. Egress enforcement, first cut

**Unblocked — ADR-0005.** The stack runs inside a dedicated WSL2 distro with Docker
Engine installed natively (never Docker Desktop's own WSL2 backend). See
`ops/wsl2/README.md` for setup and the GPU-passthrough check to run first.

`ops/nftables/` default-deny, internal-only Docker networks, internal DNS, sandbox with no
interface. Telemetry is M5 — enforcement is M0, because retrofitting it means rebuilding
the network layer under a running system.

On one box this also gets simpler: one ruleset to write and audit, and the demonstration
can be run with the network cable physically unplugged.

**Done:** a container cannot reach the internet; the sandbox has no interface at all;
the GPU box accepts exactly one inbound port and makes no outbound connection.

## 13. Remaining structural detectors

The full table in `tests/structural/AGENTS.md`. Each with a negative control.

**Done:** every detector passes clean and catches its control.

## 14. Offline install drill

Do this at M0, not at M6. Vendored wheels, pinned lockfile, no network during build.

**Done:** a clean machine with networking disabled completes `docker compose up`. Write
down what it needed — that list is the offline bundle's manifest, and discovering it on
demonstration day is the failure mode this task exists to prevent.

---

## Not in M0

Models, routing, ingestion, retrieval, the agent loop, deliverables, the UI beyond a health
page. If you are tempted, the milestone rule in `AGENTS.md` applies: M0's demonstration has
to run first.

## The four measurements M1 opens with

Queued here so they are not forgotten. Run on **both** profiles (ADR-0002).

1. **Resident set on the actual card** — `nvidia-smi` and `/api/ps`, with KV cache at the
   context lengths the loop uses. Every `vram_gb` in `registry/models.demo-local.yaml` is an
   estimate until this runs.
2. **Swap cost**, cold and page-cached, per registry entry. The router scores with this
   number; until it is measured the scoring is decorative.
3. **Tool-calling and structured-output conformance** under the runtime's JSON-schema mode.
   Decide **per capability, not globally**. The prototype was bitten by exactly this —
   empty responses under `format=json`, silently papered over. If no small model plans
   reliably on `demo-local`, the planner is the one call that routes to the larger model.
4. **Offline `docker compose up` on both boxes**, networking disabled.

---

## Progress (updated 2026-09-23)

Root `AGENTS.md` "Current state" is the authoritative version. In short:

| Task | State |
|---|---|
| 1–8 | **Done** and proven. The history is in `docs/history/m0-build-notes.md`. Task 8's second half, the chokepoint, is built: `citadel_tools.Chokepoint`, where every decision (allow and deny) is an audit event and every allow issues a receipt that the executing boundary verifies. |
| 9 | **Done.** Spans per task in `trace_spans` and metrics at `/api/metrics`, kept apart from the audit chain. |
| 10 | **Done, in substituted form**: Starlette, not FastAPI. `api`, `worker` and `sandbox` are real, and the SSE stream carries the live journal. The sandbox is a long-lived hardened service rather than an image destroyed after each run (ADR-0007). |
| 11 | **One box done** (`ops/compose/docker-compose.yml`, `citadel.cmd`, `scripts/up.sh`). The two-box override is not written. Switching the endpoint is one variable, `CITADEL_INFERENCE_ENDPOINT`. |
| 12 | **Done differently**: a default-deny ruleset inside each egress-capable container, plus internal-only networks (ADR-0006). "A container cannot reach the internet" holds. "The sandbox has no interface" became "an interface on a network with no route out, and the api/worker refuse its connections" (ADR-0007). |
| 13 | **Done.** Every detector has a negative control. `tests/deployment/` adds a check that the image contains what the code imports. |
| 14 | **Not done.** The offline install drill is still ahead, and so is its manifest. |

**M1–M5 capabilities were pulled forward**, at Fahim's request to complete the project, in
demonstration form: models and routing, ingestion with OCR and vision, ACL-filtered hybrid
retrieval, the agent loop, deliverables with verification and approval, and sovereignty
telemetry with the probe. The four M1 measurements below have **not** been run on the
real card, so the routing scores still use estimated VRAM and swap figures.
