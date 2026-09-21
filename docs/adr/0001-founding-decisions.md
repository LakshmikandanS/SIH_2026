# ADR-0001 — Founding decisions

**Status:** Accepted, partially superseded · **Date:** 2026-09-21
**Superseded in part by:** [ADR-0002](./0002-hardware-profiles-and-inference-runtime.md) — §Q1, §Q3, §Q12 and the Gap 1 resident-set table were rewritten when a 250 GB+ GPU cluster became available. **Every other section stands.**
**Resolves:** Handoff §10 Q1–Q12
**Decided by:** Fahim Ahamed (Q1, Q2, Q7, Q11, Q12 answered directly; the rest derived and open to challenge)

These twelve were taken together in one sitting because they constrain each other. Any one
of them that later changes gets its own superseding ADR; this file is not edited in place
except to add a link to the ADR that replaced a section.

Two of the decisions below **reverse a recommendation in the handoff**. They are marked
🔄 and argued rather than asserted, because reversing a written recommendation without
showing the work is how a project loses its own reasoning.

---

## The constraint that reshapes everything

The handoff designs for "a mid-range GPU… worst credible case a single 16–24 GB card."
The actual demonstration hardware is a **single RTX 5060 with 8 GB of VRAM**.

That is not a smaller version of the handoff's assumption. It is a different problem. At
24 GB you can hold a reasoning model and a vision model together and treat swapping as an
optimisation. At 8 GB you cannot, and three things follow that the handoff does not
anticipate:

1. **Model swapping cannot be engineered away. It has to be made visible and budgeted.**
2. **vLLM's memory model is disqualifying, not merely inconvenient** (§Q3).
3. **Concurrency and GPU capacity have to be decoupled**, or the multi-user demonstration
   (Q7) and the 8 GB card (Q1) contradict each other outright (§Q12).

Everything below is downstream of this.

### VRAM budget

Usable VRAM on an 8 GB card is roughly **7.3 GB** with a display attached, ~7.7 GB
headless. Run the GPU box headless.

| Resident set | Members | Est. VRAM | Use |
|---|---|---|---|
| **A — co-resident** | 4B-class reasoning (Q4) + 2–3B vision (Q4) + embedding | ~5.0–5.5 GB + 1–2 GB KV | The demo path. Everything fits, nothing swaps. |
| **B — quality tier** | 7–8B reasoning (Q4) alone | ~4.4–4.9 GB + KV | Routed to when the task's latency budget can absorb a swap. Evicts the vision model. |

These are estimates from published quantised sizes, not measurements. **Measuring them on
the actual card is the first task of M1** — see "What this commits us to" at the end.

---

## Q1 — GPU and VRAM

**Decision.** Design point is a single RTX 5060, 8 GB, Blackwell (sm_120), CUDA 12.8+.
No model, quantisation or resident-set size appears anywhere in code or in a test
assertion. All of it lives in the model registry as data, and the registry is read at
startup.

The handoff's rule — *"model size must never be an architectural assumption"* — is
enforced structurally, not by good intentions: a structural test greps the source tree for
model identifiers, VRAM constants and parameter counts outside `registry/` and fails the
build if it finds one. It ships with a negative control (a fixture file containing a
hardcoded model id, which the detector must catch) so the test cannot quietly become
vacuous.

**Consequences.** Better hardware at the venue is pure headroom and needs no code change —
it changes a registry file. Worse hardware degrades to smaller registry entries. The
routing function reads VRAM cost from the registry, so the same router works on both.

**Revisit when:** the venue hardware is confirmed, or a second GPU appears.

---

## Q2 — One box or two

**Decision. Two boxes, with the split defined by what contends for the GPU.**

| Box | Runs | Rationale |
|---|---|---|
| **GPU box** | Ollama, and nothing else | The card does one job. No CPU-heavy work competes with it. |
| **App box** | API, workers, Postgres, sandbox runner, UI, **OCR, embedding, reranking** | All CPU. All parallelisable. None of it touches the GPU. |

Moving OCR, embedding and cross-encoder reranking to the **app box CPU** is the decision
that makes the 8 GB card survivable. The handoff's §7.5 asks "whether a cross-encoder
reranker fits the latency budget on the same GPU that is serving generation." On 8 GB the
answer is no, and it does not have to: a reranker over the top ~20 candidates on CPU costs
a few hundred milliseconds and contends with nothing.

**This also improves the sovereignty proof (target E).** Two boxes means two independent
enforcement points, and the GPU box's ruleset is trivially auditable: one inbound port from
the app box, zero outbound, no DNS. A single rule an observer can read in five seconds is
better evidence than a complex one.

**Consequences.**
- A network hop now exists inside the trust perimeter. It is in scope for the threat model
  (§Q6) and it must be covered by the egress telemetry, not just the app box's.
- The demonstration needs both machines present and networked.
- **Mitigation, and the reason this is safe to commit to:** the Model Gateway talks to the
  inference runtime over HTTP and holds the endpoint in the registry. One box and two boxes
  differ by a Compose profile and a URL. If only one machine turns up at the venue, it
  collapses with no code change. This is what "make the GPU component versatile" means in
  practice, and a structural test enforces it — nothing outside the gateway may import an
  inference client or reference the inference host.

**Revisit when:** a second machine turns out not to be available, or the link latency
measurably hurts streaming.

---

## Q3 — Inference runtime 🔄

**Decision. Ollama, as the production runtime for this demonstration — not "development
only" — behind a provider interface that a vLLM backend can implement later.**

This reverses the handoff ("Replace Ollama-only inference… with Ollama acceptable for
development only"). The handoff's recommendation was correct for the hardware it assumed
and is wrong for 8 GB. The argument:

**1. vLLM's allocator makes multi-model impossible at 8 GB.** vLLM claims a fixed fraction
of the card at startup (`--gpu-memory-utilization`, default 0.9) and holds it. On a 96 GB
card one practitioner measured it taking 95.6 GB and leaving 2.3 GB, which silently pushed
a co-tenant into CPU-only execution.¹ On 8 GB, one vLLM instance owns the card. Acceptance
target A requires **at least two models selected across two task types** — vLLM at 8 GB
gives you one.

**2. Ollama already implements the residency manager §7.1 calls "the biggest technical risk
in the project."** Per Ollama's own documentation: `OLLAMA_MAX_LOADED_MODELS` defaults to
3 per GPU; a new model must fit entirely in VRAM to load alongside others; when it does not
fit, **requests queue** while idle models unload — it does not spill to CPU; and
`OLLAMA_KEEP_ALIVE` (default 5m, `-1` to pin, `0` to evict immediately) controls
residency.² That is an admission queue with a pinning policy. Citadel should **drive it,
not duplicate it**.

**3. Blackwell is a live compatibility hazard for vLLM specifically.** sm_120 needs CUDA
12.8; vLLM's stable wheels have not pinned tightly enough against it, producing
`libcudart.so.12` import failures and requiring nightly builds.¹ llama.cpp/Ollama CUDA
builds handle sm_120 without that fight. "Hard to reverse after M1" (handoff Q3) cuts both
ways — do not spend M1 on dependency archaeology.

**What we give up, stated honestly.** vLLM is materially better where it fits: measured
TTFT 33 ms vs Ollama's 129 ms, prefill ~20,300 vs ~5,960 tok/s, and 2.8× the throughput at
4 concurrent requests.¹ Those are real and they matter for agent loops. We are trading
them for the ability to hold more than one model at all. On this card that is not a close
call.

**Consequences.**
- The `InferenceProvider` interface is written first and Ollama implements it. A vLLM
  backend is a new implementation, not a refactor. Monarch already carries the same shape
  (`monarch.llm.provider.LLMProvider`), so the two repositories stay idiomatically
  consistent.
- The interface must expose **streaming** from day one. Monarch's ABC does not
  (`generate` only, with a comment anticipating `stream`); Citadel's must, because the
  handoff requires streaming token output from every generation call.
- Structured output uses Ollama's JSON-schema `format` parameter. §7.3 records the
  prototype's `qwen3:4B` returning empty responses under `format=json` — **that benchmark
  is still owed** and is listed below.

**Revisit when:** the deployment hardware reaches ~24 GB or more, or concurrent-user load
makes Ollama's throughput the binding constraint.

---

## Q4 — Postgres or SQLite

**Decision. Postgres from M0, with `pgvector` as the vector index.**

Q7's answer settles what the handoff left open. Multiple engineers working in parallel
plus a separate approver is a concurrency workload; SQLite was already the root of the
prototype's concurrency problems, and a `threading.Lock` around it is explicitly on the
Replace list.

**pgvector rather than Qdrant or LanceDB**, for one reason that dominates: the retained
principle is **authorization filtering before ranking**, and in Postgres that is a `WHERE`
clause in the same query as the vector search — the database enforces it, not application
code that could be bypassed. It also keeps all state on the app box in one engine, which
matters more now that the GPU box is separate.

**The gotcha, recorded now so it is not discovered at M2.** pgvector's HNSW index with a
restrictive filter will silently return **fewer rows than requested** unless iterative
index scans are enabled (`hnsw.iterative_scan`, pgvector 0.8.0+), because the index walk
terminates before enough rows survive the filter.³ On an ACL-filtered corpus, filters are
restrictive by design. **This must be a test, not a comment:** a fixture where a
low-clearance user's filter excludes most of the corpus, asserting that the surviving
result count is still `k`.

**A subtlety the handoff misses.** The prototype's `_passes` filters correctly in the sense
that matters — a denied chunk "is simply never constructed into a result in the first
place" — but it does so in Python, over an in-memory list. Moving to an index changes the
mechanism: the predicate becomes SQL. That is stronger, but it silently loses something
the prototype produced, namely the `DeniedDocument` record (document id, classification,
ACL, reason — deliberately never the text) that gives an auditor the denial count. **Keep
it.** It costs one extra aggregate query and it is the only evidence that ACL filtering
ran at all. Without it, "ACL filtering works" is unfalsifiable — and the Q7 demonstration
is specifically about showing that it does.

**Revisit when:** the corpus exceeds roughly ten million chunks, or filtered-query latency
misses the budget after HNSW tuning.

---

## Q5 — Governance scope for v1

**Decision. Wider than the handoff recommends, because Q7's answer demands it.**

| Surface | Milestone | Why |
|---|---|---|
| Classification lattice, ACLs | **M0** | Cheap now, expensive to retrofit. Unchanged from the handoff. |
| Audit chain | **M0** | Same. |
| **Identity, roles, per-user ACL filtering** | **M0** (was M5) | Q7 makes ACL filtering a demonstrated capability, not a background property. It cannot arrive at M5. |
| Approval workflow | **M4** | Unchanged. |
| Signed decision receipts | **M3–M4**, scoped to the sandbox and data boundaries only | Unchanged. |

**Port `contracts/` early and wholesale.** It is a genuinely self-contained package —
`classification.py`, `domain.py`, `envelopes.py`, `events.py`, `receipts.py`,
`state_machines.py`, with a 13 KB adversarial test for receipts and a structural test
(`test_contracts_is_self_contained.py`) that enforces it has no inward dependencies. That
last test is why it ports cleanly: the property was enforced, not hoped for.

Two changes on the way in, both from the handoff's Adapt list: the **event vocabulary
becomes open** (a registry, not a closed set of sixteen that rejects anything new), and
signing key material gets a documented lifecycle instead of a per-process random secret.

**Revisit when:** the demonstration scope narrows, or receipts prove to cost more than the
boundary they guard is worth.

---

## Q6 — Threat model ⚠️ *unconfirmed — please confirm or correct*

**Proposed.** Keep the handoff's model: hostile content in ingested documents, a malicious
insider, and an attacker with code execution inside the sandbox. Host compromise remains
out of scope.

**Add one, forced by Q2:** the **GPU box ↔ app box link**. Two boxes create an in-transit
boundary that did not exist in a single-box design. Minimum treatment: the link carries no
classified document text by construction — the app box sends prompts and receives tokens —
and the link is covered by egress telemetry on both ends, so an unexpected flow on it is
recorded the same way an outbound attempt would be.

This is the one question below where nothing has been confirmed. If the demonstration
threat model is narrower than this, the receipt machinery in Q5 should be cut hard rather
than kept out of inherited habit.

---

## Q7 — Users at demonstration time

**Decision. Multiple engineers submitting in parallel, plus a distinct approver, with
per-user ACL filtering visibly in effect.** (Answered directly.)

This is the most consequential of the four answered questions, because it moves identity,
roles and ACL filtering from M5 to M0 (§Q5), forces Postgres (§Q4), and forces the
concurrency design in §Q12.

**What it needs to be demonstrable, not merely implemented.** ACL filtering is invisible
when it works — the user simply does not see a document. That is not a demonstration. It
needs a deliberate surface, the same way the sovereignty panel is a deliberate surface for
target E: **two engineers, different clearances, the same query, visibly different
citation sets, with the denial count shown** (which is what the `DeniedDocument` record in
§Q4 is for). Build the surface, not just the filter.

Minimum cast for the demo: two engineers at different classification levels in different
departments, and one approver. Three identities.

---

## Q8 — Handwriting

**Decision. Out of scope for v1 beyond handwritten form fields.** Unchanged from the
handoff's recommendation, and reinforced by the hardware: the vision models that read free
handwritten prose acceptably do not fit in the resident set (§Q1).

Printed and stamped content plus handwritten *form fields* is in scope. Free handwritten
prose is a stretch goal and is **not** in any acceptance-target path.

Say this in the demonstration rather than hoping nobody writes on a page. "Degrade
honestly" (handoff §3.2) means low-confidence regions are flagged as low-confidence, not
silently guessed at.

---

## Q9 — How Monarch is consumed

**Decision. An installed, version-pinned package. Not a submodule, not vendored.**

Monarch is already a proper package — `src/` layout, `pyproject.toml`, setuptools backend,
console entry points, `requires-python >=3.12`. Nothing needs to change to depend on it.
Pin an exact version; vendor the built wheel into the offline install bundle (§7.8).

**The finding that matters: the seam the handoff describes does not exist yet.**

The handoff states the constraint as though it were already true — *"Monarch receives an
opaque scope key plus a caller-supplied visibility predicate, and never learns Citadel's
classification levels or ACL vocabulary."* Reading the code, neither half is there:

- `Memory` (`memory/models.py`) has `memory_id`, `content`, `status`, five enums and
  access bookkeeping. **There is no scope, tenant or owner field.**
- `retrieve_memories(query, top_k)` (`memory/retrieve.py`) takes **no predicate**. It calls
  `repository.search_similar(embedding, top_k=top_k*3)` — a global search over one store —
  then filters `status == ACTIVE` **in Python, after retrieval**.
- `repository` and `config` are module-level singletons (`from monarch.config import
  TOP_K, RECENCY_DECAY, RECENCY_WEIGHT`), so there is no per-caller context to thread a
  scope through.

That last point is the sharp one. **Monarch post-filters.** Citadel's first retained
principle is that post-filtering is strictly weaker than filtering before ranking. Wiring
Citadel's memory tiers to Monarch as it stands would import into the memory path exactly
the pattern Citadel refuses in the retrieval path — and memory is where organisational
facts about equipment, vendors and people accumulate, which is not less sensitive than the
document corpus.

**So the seam is an upstream change to Monarch, not a Citadel-side adapter.** Concretely:

1. A `scope_key` on the memory record, opaque to Monarch, indexed.
2. A visibility predicate accepted by `retrieve_memories` and **pushed into the LanceDB /
   SQLite query**, not applied after. Filter before rank, same rule, same reason.
3. Repository and config injected per call or per session rather than module-global,
   without which (1) and (2) have nowhere to live.

Until those land, **Citadel uses working memory only** (task-scoped, Citadel-side,
Postgres) and the episodic and semantic tiers stay behind an interface with no
implementation. M5 is the milestone that needs them; that is the deadline for the Monarch
change. This does not block M0–M4.

**On the taxonomy.** `MemoryType` is confirmed person-centric — `USER_PREFERENCE`,
`PERSONAL_FACT`, `GOAL_OR_PLAN`, `TECHNICAL_SKILL`, `EPISODIC` — and so are `Predicate`
(`PREFERS`, `LIVES_IN`, `STUDIES_AT`…) and `ObjectType`. The handoff asks whether extending
these is a Monarch change or a Citadel-side one. **Neither: make the vocabulary open**, the
same fix as Citadel's own closed event vocabulary (§Q5). A closed `StrEnum` that rejects
`EQUIPMENT_FACT` is the identical mistake as an event vocabulary closed at sixteen. Monarch
should accept a registered vocabulary; Citadel registers its terms at startup and neither
repository learns the other's domain.

**Consequences.** Monarch brings SQLite + LanceDB alongside Citadel's Postgres, so the app
box runs two storage engines from M5. That is acceptable — it is the price of the seam, and
the seam is the point. Do not merge the repositories to avoid it; that constraint is hard.

**Revisit when:** the Monarch changes land, or M5 arrives without them.

---

## Q10 — Frontend stack

**Decision. Vite + React + TypeScript, Tailwind, and components copied into the source
tree rather than installed from a registry at build time.**

The binding constraint is §3.2: no internet **at build time** either. That rules out any
component library resolved from a network registry during a build, and favours the
copy-into-your-repo model (shadcn/ui-style) — the components become ordinary files in the
repository, reviewable, patchable, and present on a disconnected machine because they are
committed.

Offline path: commit the lockfile, `npm ci` against a vendored tarball cache (or a local
Verdaccio in the bundle), self-host fonts and icons, no CDN references anywhere. A
structural test greps the built assets for external URLs and fails on any hit — the same
detector-plus-negative-control pattern as everything else, and it doubles as evidence for
target E.

**Streaming: SSE, not WebSockets.** The task stream is one-directional; SSE reconnects on
its own, survives proxies, and the handoff's architecture diagram already says HTTP + SSE.
Cancellation is an ordinary POST.

**Revisit when:** a UI surface needs genuine bidirectional traffic.

---

## Q11 — Document templates

**Decision. Construct a realistic template set.** (Answered directly.) Three to start:
an approval note, an inspection summary, and a calculation sheet — each with letterhead,
classification markings, an approval block, and revision history.

The risk in a constructed template is that it gets built to whatever python-docx does
easily, which quietly redefines the target. Guard against it: fix the template first, by
hand, in Word, as an artefact nobody is allowed to simplify; then make the generator match
it. §7.6 asks how much fidelity python-docx can reach against a real sample — that question
only has meaning if the sample is fixed first.

Templates go in a registry with declared placeholders, so a real organisational template
can replace a constructed one without a code change. That is the property that matters,
and it survives the templates being invented.

---

## Q12 — Concurrency target

**Decision. Task concurrency is unbounded; GPU admission is bounded. They are separate
mechanisms.** (Q7 answered directly; this is the design that follows.)

Q1 and Q7 are in direct tension: multiple engineers working in parallel, on a card that
holds one resident set. Resolving it by serialising whole tasks would make the concurrency
demonstration a lie. The resolution is that **most of an agentic task is not GPU work**:

| Runs freely in parallel (app box, CPU) | Bounded by GPU admission |
|---|---|
| Ingestion, OCR, layout analysis | Planning and replanning |
| Embedding, BM25, fusion, reranking | Reasoning steps |
| Sandbox code execution | Vision extraction |
| Document generation, verification | — |
| Approval workflow, audit writes | — |

A semaphore in front of model calls, sized to the resident set, is the whole mechanism.
Workers pull tasks freely; they queue only at the model call. Queue wait is charged to the
task's wall-clock budget and shown in the UI, so a waiting task is visibly waiting rather
than apparently hung.

**This is what makes the honest claim honest:** three engineers' tasks genuinely progress
in parallel, and the demonstration can say exactly where they serialise and why. That is a
better story than pretending an 8 GB card is doing three reasoning passes at once.

**Consequence for the router.** Because GPU time is the scarce resource, **residency
becomes a scored term in the routing function**: a resident model scores higher than one
requiring a swap, and the estimated swap cost appears in the score breakdown.

This turns §7.1's risk into acceptance target A's best evidence. The handoff wants the
breakdown visible to show *why* a model was chosen; a breakdown that reads
`capability match +40 · quality tier +25 · resident ✓ +15` against
`capability match +45 · quality tier +35 · requires swap ~8s −30` shows a router reasoning
about real physical constraints. That is considerably more convincing than a router
choosing between two models that both happened to be loaded.

---

## Checked back against the acceptance targets

Each decision was re-read against targets A–E to find any that makes a target unreachable,
and any target with no decision supporting it. Two gaps surfaced; both are resolved here
rather than left for M6 to discover.

| Target | Supported by | Status |
|---|---|---|
| **A** — model auto-selection, visible | Q3 (multi-model at all), Q1 (registry as data), Q12 (residency in the score breakdown) | ✅ with the fix below |
| **B** — scanned report → Word approval note | Q11 (templates), Q4 (citations with page + bbox), Q5 (approval at M4) | ✅ |
| **C** — coding task verified in a sandbox | Q2, Q12 (sandbox is app-box CPU, no GPU contention, runs fully parallel) | ✅ |
| **D** — multimodal understanding | Q1 resident set, Q2 (OCR on app-box CPU) | ✅ with the fix below |
| **E** — zero external calls | Q2 (two enforcement points), Q10 (build-asset check) | ✅ |

**Gap 1 — target A needs two reasoning models resident at once, not one.** Acceptance
target A is a code-generation task and a document task routing to *different* models. If
resident set A holds a single reasoning model, the router has nothing to choose between and
the demonstration reduces to a swap. Revised resident set A:

| Member | Est. |
|---|---|
| 4B-class general reasoning | ~2.6 GB |
| 3B-class coding model | ~1.9 GB |
| Embedding (or app-box CPU) | ~0.3 GB |
| KV cache, 2–3 parallel | ~1.5 GB |
| **Total** | **~6.3 GB of ~7.3 GB** |

Tight, and the first thing measurement (1) below must confirm. The fallback if it does not
fit: move embedding to the app box CPU entirely, which it can be — that is the point of Q2.

**Gap 2 — vision must run at ingest, not inside the agent loop.** §7.1's specific fear is a
model swap "landing in the middle of a step." With two reasoning models resident, the vision
model is the one that swaps. The fix is scheduling, not capacity: **OCR and vision extraction
happen at ingest time**, when the user uploads the document, and their output — the
normalised page/block model with bounding boxes — is persisted. By the time the agent loop
runs, vision has already happened and the loop reads rows, not pixels.

This costs nothing, because the handoff's own pipeline already ingests before it reasons
(§5.1 steps 1–2). Making it an explicit rule means the swap happens while a progress bar is
on screen instead of mid-step, and it removes the single largest demonstration risk in §7.
`vision.extract` remains available as a tool for the case where the loop genuinely needs to
re-read a region, and that call is budgeted as a swap.

## What this commits us to before M1

Four measurements. None is optional, and all four are cheap relative to discovering them at
M6.

1. **Measure the resident set on the actual card.** Load sets A and B, read
   `nvidia-smi` and Ollama's `/api/ps`, and record what actually fits with KV cache at the
   context lengths the agent loop uses. Every number in the VRAM table above is an estimate
   until this runs.
2. **Measure swap cost**, cold and page-cached, for each registry entry. The router scores
   with this number; until it is measured the scoring is decorative.
3. **Benchmark tool-calling and structured-output conformance** on the candidate models
   under Ollama's JSON-schema `format`. §7.3 is unresolved and the prototype has already
   been bitten by it once. Published small-model tool-calling benchmarks are not
   reassuring — the best sub-4B models tested still showed keyword-triggered tool calls,
   resistance to negation, and failure to notice information already present in the
   prompt.⁴ **Decide per capability, not globally**, and if no small model is reliable
   enough to plan, the planner is the one call that routes to resident set B.
4. **Prove `docker compose up` on both boxes with networking disabled**, at M0, not at M6.

## Model downloads — waiting on you

Per your instruction, nothing has been pulled. The candidate set I would benchmark:

- **Reasoning / tool-calling:** a 4B-class instruct model, plus one 7–8B for resident set B
- **Vision:** a 2–3B document-oriented VLM (the IBM Granite Vision and Qwen-VL families are
  the two worth testing at this size)
- **Embedding:** a small CPU-friendly text embedder, running on the app box
- **Reranking:** a cross-encoder, also app box CPU

Tell me which exact tags you want and I will pin them into the registry; or say the word
and I will propose specific tags with sizes for you to approve.

---

### Sources

1. [vLLM or Ollama on Blackwell: benchmarks and landmines](https://allenkuo.medium.com/vllm-or-ollama-on-blackwell-benchmarks-landmines-and-what-agents-actually-need-5dc539bb28ef)
2. [Ollama FAQ — concurrency, model loading and keep-alive](https://docs.ollama.com/faq)
3. [pgvector vs Qdrant vs LanceDB for on-premise RAG](https://iotdigitaltwinplm.com/pgvector-vs-qdrant-vs-lancedb-2026/)
4. [Local Agent Bench — 21 open-weight models on tool calling](https://mikeveerman.be/blog/github-2026-02-06-tool-calling-benchmark/)

Monarch and prototype findings are from reading `AI_WORKBENCH/MONARCH/src/monarch/` and
`AI_WORKBENCH/CITADEL/{contracts,app/rag/search.py}` directly.
