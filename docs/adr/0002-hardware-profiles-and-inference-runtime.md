# ADR-0002 — Hardware profiles and inference runtime

**Status:** Accepted · **Date:** 2026-09-21
**Supersedes:** ADR-0001 §Q1, §Q3, §Q12 (and the "Gap 1" resident-set table)
**Leaves standing:** every other section of ADR-0001
**Amended by:** [ADR-0003](./0003-the-hpc-cluster-is-not-the-demonstration.md) — the cluster turned out to be the university's, so its open question is closed and the profiles were renamed to carry their role: `dev-8gb` → **`demo-local`**, `cluster` → **`hpc-eval`**. Read the names below as those. The runtime reasoning is unchanged and now rests on firmer ground: H200 is Hopper, so none of the Blackwell wheel problems apply to the vLLM side.

---

## Context

ADR-0001 was written against a single RTX 5060 with 8 GB of VRAM, and reasoned hard from
that constraint — it reversed the handoff's inference-runtime recommendation specifically
because vLLM's allocator makes multi-model impossible at 8 GB.

A GPU cluster with **250 GB+ of allocatable VRAM** is now available for this project. The
RTX 5060 remains, and remains the machine development actually happens on.

This is not "the constraint went away." It is the arrival of a second, very different
target — which is the situation the handoff's own rule was written for:

> *model size must never be an architectural assumption.*

That rule has been abstract until now. It is now testable, because there are two real
profiles that must both work from one codebase.

---

## Decision

### 1. Two named hardware profiles, both first-class

| | `dev-8gb` | `cluster` |
|---|---|---|
| Hardware | RTX 5060, 8 GB, sm_120 | 250 GB+ allocatable |
| Runtime | **Ollama** | **vLLM** |
| Residency | Swapping is normal; admission is tight | Everything resident; no swapping |
| Concurrency | GPU admission ≈ 2 | GPU admission ≈ workers |
| Purpose | Daily development, correctness, offline drills | Demonstration, evaluation, load |

Neither is "the real one." A change that works on only one of them is not done.
CI runs the structural and unit suites with no GPU at all, integration against `dev-8gb`.

**The profile is a registry selection, not a code path.** `CITADEL_PROFILE` selects which
registry files load. There is no `if profile == "cluster"` anywhere outside the gateway's
provider construction, and a structural test enforces it.

### 2. ADR-0001 §Q3 amended: both runtimes, chosen by profile

ADR-0001 reversed the handoff and made Ollama the production runtime. With a cluster in
play that reversal narrows to what it always really was — **a statement about 8 GB cards**.

- **`cluster` → vLLM.** The handoff's original reasoning applies and is correct at this
  scale: proper batching, continuous batching, far better TTFT and concurrent throughput.
  The measured gaps cited in ADR-0001 (TTFT 33 ms vs 129 ms; ~20,300 vs ~5,960 tok/s
  prefill; 2.8× throughput at 4 concurrent requests) are arguments *for* vLLM once memory
  is not the binding constraint. At 250 GB it is not.
- **`dev-8gb` → Ollama.** Unchanged, for every reason ADR-0001 gave: dynamic load/unload,
  queue-when-full, keep-alive pinning, and no fight with sm_120 wheels.

**Consequence: `InferenceProvider` stops being insurance and becomes the load-bearing
seam.** It was already written first in ADR-0001; now two implementations ship on day one,
which is the only way to know the abstraction is real. An interface with one implementation
is a guess.

The interface must carry, from M1:

- streaming generation (non-negotiable — handoff §3.1)
- structured output against a JSON schema
- a residency query: which models are loaded, right now
- an explicit load / pin / evict, or a documented no-op where the runtime owns it
- per-call model override
- token accounting returned with every response, for budgets and traces

Where a runtime cannot honour one of these, the provider says so explicitly rather than
pretending. `supports()` returns capabilities; the gateway records a degraded provider
rather than silently working around it (handoff §3.2: *degrade honestly*).

### 3. ADR-0001 §Q1 amended: the VRAM table is now per profile

The `dev-8gb` budget from ADR-0001 stands unchanged and is still the tighter design
constraint, so it is the one that shapes the code. The `cluster` profile holds the full set
resident — reasoning, coding, vision, embedding and reranker — with room for tensor
parallelism and a large context.

**Design to `dev-8gb`, demonstrate on `cluster`.** Designing to the cluster and hoping it
degrades is how the 8 GB box stops working in week three, and the 8 GB box is where the
work happens.

### 4. ADR-0001 §Q12 amended: admission is configured, not constant

The mechanism is unchanged and was always right: task concurrency unbounded, GPU admission
bounded by a semaphore. Only the number moves, and it comes from the profile registry.

The split in ADR-0001 §Q12 — ingestion, OCR, embedding, retrieval, sandbox and document
generation running free while planning, reasoning and vision queue — stays exactly as it
is. On `cluster` the semaphore is wide enough that nothing waits in practice, and the code
does not know the difference.

### 5. Two ADR-0001 fixes are kept even though the cluster makes them unnecessary

- **Two reasoning models resident** (general + coding). Trivial on the cluster, required on
  `dev-8gb`, and it is what makes acceptance target A a real choice rather than a swap.
- **Vision runs at ingest, not inside the agent loop.** The cluster removes the swap cost
  that motivated this, but the rule is better architecture regardless: extraction is
  idempotent, cacheable, re-runnable when a better model lands, and it keeps page/block
  output pinned to a document version rather than to a task. Keep it.

---

## ⚠️ The question the cluster raises — answer before M1

**Where is this cluster, and who administers it?**

The entire product claim is that nothing leaves the premises, and acceptance target E is
the proof of it. If the 250 GB allocation is a rented cloud GPU, a shared university or
institutional cluster reached over the public internet, or anything the organisation does
not physically control, then:

- Confidential document text crosses a network the organisation does not own, on every
  single model call.
- Acceptance target E cannot honestly be demonstrated on that configuration. The
  sovereignty panel would be measuring the app box while the actual data egress is the
  inference call itself.

This is not a small caveat. It inverts the demonstration.

**Three outcomes, and what each means:**

| The cluster is… | Then |
|---|---|
| On-premises, organisation-administered, inside the sovereign perimeter | Use it for everything. The link is an internal boundary (ADR-0001 §Q6) and telemetry covers it. Nothing else changes. |
| Off-premises or third-party | **Development and evaluation only.** Never the sovereignty demonstration, never real confidential material. Target E is demonstrated on the local box. Say so on the slide. |
| Unknown | Treat as off-premises until confirmed. Fail closed — the same rule the classification lattice uses for unknown markings. |

**Flagged for Fahim. Nothing below depends on the answer, so work proceeds either way, but
the demonstration script does.**

---

## What does not change

Everything else in ADR-0001 stands: two boxes (§Q2), Postgres with pgvector and the
iterative-scan requirement (§Q4), governance scope with identity and ACL at M0 (§Q5), the
threat model still awaiting confirmation (§Q6), the multi-user ACL demonstration surface
(§Q7), handwriting out of scope (§Q8), Monarch as a pinned package with the scope seam
owed upstream (§Q9), the offline-buildable frontend (§Q10), and constructed templates
(§Q11).

The four pre-M1 measurements in ADR-0001 still stand, now doubled: run them on **both**
profiles. The `cluster` numbers will be boring. The `dev-8gb` numbers are the ones that
constrain the design, and boring cluster numbers are themselves the evidence that the
profile abstraction works.

---

## Revisit when

- The cluster's location and administration are confirmed (see above).
- vLLM's sm_120 support stabilises enough to use it on the dev box too, collapsing two
  runtimes into one.
- A third profile appears — at which point check that adding it required only registry
  files, and if it did not, this ADR failed.
