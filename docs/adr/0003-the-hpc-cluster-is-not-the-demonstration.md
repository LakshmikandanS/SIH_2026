# ADR-0003 — The HPC cluster is not the demonstration

**Status:** Accepted · **Date:** 2026-09-21
**Answers:** the open question in ADR-0002 ("Where is this cluster, and who administers it?")
**Amends:** ADR-0002 §1 (profile names and roles), ADR-0001 §Q2, §Q6

---

## The answer

The 250 GB allocation is on the **university's HPC cluster**, reached over SSH via
MobaXterm. An NVIDIA DGX-H200 (8 × H200, 141 GB each), alongside x86 compute nodes, an
AC922 with A100, Tesla K80 nodes, an IBM IC922 Power9 server with V100, and Dell Unity /
IBM storage explicitly described as holding *student and faculty projects and datasets*.

That is ADR-0002's second branch, unambiguously: **off-premises, third-party administered,
shared with other users.** Not the organisation's own GPU server, not Fahim's, and reached
across a network neither controls.

So the decision that follows is forced:

> **Acceptance target E is demonstrated on the local box. Never on the cluster.
> No confidential material is ever staged to cluster storage.**

## Why this is good news, not a setback

It is tempting to read "our big GPU can't host the demo" as a loss. It is the opposite,
because the problem statement already told us where the demonstration lives:

> *"A working local deployment, demonstrable on a single workstation or server with a
> mid-range GPU (use a smaller open weight model if 120B class hardware isn't available at
> the venue)."*

The demonstration was **always** meant to be a single box. The RTX 5060 is not a fallback
we are stuck with — it is the target the problem statement describes. The cluster was never
the demo machine, and discovering that now rather than in week five is worth more than the
VRAM.

And the cluster gives us something a bigger demo box never could. The problem statement
also asks that *"new open weight models should be addable later without redesigning the
system."* That claim is ordinarily unfalsifiable in a demo. With two profiles it becomes a
thing we can show:

> Here is Citadel running sovereign on an 8 GB card, with nothing leaving the box.
> Here is the **same code, same registries, no changes**, driving 32B-class models on
> 8 × H200. Model size is a registry entry, not an architectural assumption.

That is a stronger slide than either machine alone, and it is the direct answer to the
"multi-model, extensible" axis of the problem statement.

---

## Decisions

### 1. Profiles are renamed to carry their role

ADR-0002's names were dangerously neutral. `dev-8gb` reads as "the scratch one" and
`cluster` reads as "the real one" — exactly backwards, and a misreading that would quietly
sink target E. Renamed:

| Old | New | Role |
|---|---|---|
| `dev-8gb` | **`demo-local`** | RTX 5060, Ollama, single box. **The demonstration.** Sovereign. Target E lives here. Real corpus. |
| `cluster` | **`hpc-eval`** | DGX-H200 via SLURM, vLLM. **Evaluation and benchmarking only.** Never sovereign, never confidential. |

`demo-local` is the profile the product is. `hpc-eval` is a measuring instrument.

### 2. The data rule is mechanical, not a promise

A note saying "don't put confidential documents on the cluster" is a note. Instead, the
profile carries a **classification ceiling**, enforced at ingest:

```yaml
hpc-eval:
  classification_ceiling: public      # ingest REFUSES anything above this
```

Citadel already rejects documents whose classification exceeds a ceiling — that machinery
exists for tools (`registry/tools.yaml`) and it is the same lattice comparison. Applying it
at the profile level means the `hpc-eval` deployment **cannot** ingest restricted material
even if someone tries. Fail closed, same as an unknown marking.

This costs nothing, because the v1 corpus and templates are constructed anyway
(ADR-0001 §Q11). The synthetic corpus is the *only* thing the cluster ever sees.

### 3. Docker Compose does not extend to the cluster

HPC clusters do not give users root, so there is no Docker daemon. The standard is
**Apptainer / Singularity**: no daemon, integrates with the scheduler, runs from a `.sif`
image built elsewhere.¹

This is a real divergence and it is confined by design: `hpc-eval` runs **only the
inference runtime**, exactly as the GPU box does on `demo-local` (ADR-0001 §Q2). The app
box — Postgres, workers, API, sandbox, OCR — stays on Docker Compose and stays local. So
the cluster needs one `.sif` containing vLLM, not a port of the whole system.

The two-box split done for VRAM reasons turns out to be what makes this cheap. That is
usually a sign the split was along the right seam.

### 4. The endpoint is ephemeral, and the gateway already assumes it is not

SLURM allocates a compute node per job, so the vLLM endpoint is a different hostname every
run, and the job ends at the wall-clock limit whether or not you are finished.¹

ADR-0001 §Q2 put the inference endpoint in the registry and required that nothing outside
the gateway reference an inference host. That decision was made so one box and two boxes
would differ by a URL. It now also means a *per-allocation* URL costs nothing: the job
writes its endpoint, the registry is updated, the gateway reads it. Nothing else in the
codebase learns that the endpoint moved.

Practical mechanics are in `ops/hpc/README.md`.

### 5. Use the DGX-H200 nodes. Avoid the rest.

The cluster is heterogeneous and two of its parts are traps.

| Node | Verdict |
|---|---|
| **DGX-H200** (8 × H200, x86_64, Hopper sm_90) | **Use this.** vLLM's best-supported target. 141 GB per card. No wheel archaeology. |
| **x86 compute nodes** | Useful for CPU-side evaluation — OCR CER, embedding, reranking — which is where that work runs anyway (ADR-0001 §Q2). |
| **AC922 / IC922** (IBM POWER9) | **Avoid.** These are `ppc64le`. vLLM must be built from source there, with patched LLVM, Triton and PyTorch; IBM's own writeup calls the manual path *"complex and error-prone"*.² There is no reason to spend a week on this when x86 H200s are sitting there. |
| **Tesla K80** | **Avoid.** Kepler, compute capability 3.7, long dropped by current CUDA and PyTorch. |

A realistic allocation of ~250 GB is **2 × H200**, so `hpc-eval` is configured with
`tensor_parallel_size: 2` and treats more as headroom.

---

## What the cluster is for

Concretely, so it does not become a vanity resource:

1. **The reference half of the four M1 measurements** (ADR-0001). The `demo-local` numbers
   constrain the design; the `hpc-eval` numbers prove the profile abstraction holds. Boring
   cluster numbers are the result we want.
2. **Model benchmarking — handoff §7.3, still the most dangerous unresolved gap.** JSON
   schema conformance under load, multi-turn tool calling, long-context retention,
   instruction adherence for drafting. 141 GB lets us test candidates we could never hold
   locally, then pick the small model that best approximates the good one.
3. **The eval harness (§8.3).** Routing confusion matrix, retrieval recall@k and MRR,
   citation precision, OCR CER and field F1, grounding rate, and the 20-task rubric suite.
   This is the cluster's highest-value use by a distance.
4. **A judge model.** Rubric-grading 20+ tasks across several candidate models by hand does
   not scale and will not get done. Running a large model on the H200 as the grader is what
   makes the rubric suite actually run in CI. The judge never touches confidential material
   — the eval corpus is synthetic by construction (§2 above).
5. **Failure drills at scale** — model unavailable, fallback chain exercised, budget
   exhaustion — cheap to run when capacity is not the constraint.

## What the cluster is never for

- Acceptance target E, or any sovereignty claim.
- Any real, confidential, or customer-provided document.
- Anything on the demonstration critical path. **If the demo depends on SSH to a
  university cluster, the demo has a single point of failure the venue controls.**

---

## Consequences

- ADR-0002's open question is closed. Its profile table and both `models.*.yaml` files are
  updated; its reasoning about vLLM-versus-Ollama is unchanged and now has firmer ground,
  since H200 is Hopper and none of the Blackwell wheel problems apply.
- **ADR-0001 §Q6 (threat model) gains an item.** ADR-0002 added the app-box ↔ GPU-box link.
  The `hpc-eval` link is different in kind: it leaves the perimeter entirely. It is handled
  not by securing it but by ensuring nothing worth protecting crosses it — which is what
  the profile classification ceiling enforces.
- The eval harness must **checkpoint and resume**. A wall-clock limit will kill a long
  rubric run, and an eval suite that cannot survive that will silently stop being run.
- `ops/hpc/` is a new area: the Apptainer definition, the sbatch script, and endpoint
  publication.

## Revisit when

- The organisation's own GPU server exists, at which point `hpc-eval` keeps its evaluation
  role and a third profile becomes the sovereign deployment — and adding it should require
  only registry files. If it requires more, ADR-0002 failed and this is where we find out.
- The venue turns out to provide hardware better than the RTX 5060, which changes
  `demo-local`'s registry entries and nothing else.

---

### Sources

1. [Running GPT-OSS with vLLM on supercomputers — SLURM + Singularity](https://medium.com/@qualis2006/running-gpt-oss-with-vllm-on-supercomputers-slurm-singularity-a6ab70f7e044)
2. [LLM inference with vLLM using GPU on Power9 — IBM Community](https://community.ibm.com/community/user/blogs/lucas-sousa-pereira/2026/04/30/llm-inference-with-vllm-using-gpu-on-power9)
