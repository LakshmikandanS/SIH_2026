# Running the `hpc-eval` profile on the university HPC

**Read `docs/adr/0003-the-hpc-cluster-is-not-the-demonstration.md` first.** The cluster is
a measuring instrument. It is never sovereign, never sees confidential material, and is
never on the demonstration critical path.

This directory holds the Apptainer definition, the sbatch script and the endpoint
publication mechanism. Nothing else from Citadel runs here: the cluster serves **inference
only**, exactly as the GPU box does on `demo-local`.

---

## What is different from `demo-local`

| | `demo-local` | `hpc-eval` |
|---|---|---|
| Container runtime | Docker Compose | **Apptainer / Singularity** |
| Inference endpoint | Static hostname | **Different compute node every job** |
| Lifetime | Runs until stopped | **Ends at the wall-clock limit** |
| Reaching it | Same network | **SSH tunnel from MobaXterm** |
| Model weights | `ollama pull` | Staged to scratch, cached in `$HF_HOME` |

### Why Apptainer and not Docker

HPC clusters do not grant users root, so there is no Docker daemon. Apptainer runs from a
`.sif` image with no daemon and integrates with the scheduler. Build the `.sif` elsewhere
(or convert a Docker image) and copy it to scratch — you cannot build it on the login node
either.

### Why only the inference runtime runs here

The app box — Postgres, workers, API, sandbox, OCR, embedding, reranking — stays on Docker
Compose, locally. That is ADR-0001 §Q2's two-box split, and it is what keeps this whole
directory to one container instead of a port of the system.

---

## Node selection

**Use the DGX-H200.** x86_64, Hopper sm_90, 141 GB per card. vLLM's best-supported target.

**Do not use:**

- **AC922 / IC922** — IBM POWER9, `ppc64le`. vLLM needs a from-source build there with
  patched LLVM, Triton and PyTorch; IBM's own writeup calls the manual path *complex and
  error-prone*. There is no reason to spend a week on it with x86 H200s available.
- **Tesla K80** — Kepler, compute capability 3.7, dropped by current CUDA and PyTorch.

The **x86 compute nodes** are genuinely useful for the CPU side of evaluation — OCR
character error rate, embedding throughput, reranker latency — which is where that work
runs in production anyway.

---

## The shape of a run

1. **Stage weights once.** Set `HF_HOME` to scratch (e.g. `/scratch/$USER/.huggingface`)
   so the cache survives between jobs and is not re-downloaded per allocation. Compute
   nodes often have no outbound internet — if so, stage from the login node first.
2. **Submit.** `sbatch ops/hpc/vllm-server.sbatch` requesting the H200 partition, 2 GPUs,
   and a wall-clock limit that covers first-run graph capture as well as the work.
3. **Publish the endpoint.** The job writes its compute node hostname and port to a file
   on scratch. `CITADEL_HPC_ENDPOINT_FILE` points at it, and the gateway reads it.
   Nothing else in the codebase learns that the endpoint moved — that is ADR-0001 §Q2's
   registry-held endpoint paying off.
4. **Tunnel.** From MobaXterm, forward the vLLM port from the compute node to localhost.
   Have the sbatch script emit the exact `ssh -L` line to a file; typing it by hand once
   per allocation is how a wrong port number costs twenty minutes.
5. **Run the eval harness** against the tunnelled endpoint.
6. **Release.** Do not sit on an idle allocation — it is shared infrastructure.

---

## Gotchas, in the order they will bite

- **First run is slow.** Weight download, CUDA graph capture and compilation all land on
  the first invocation. Budget wall-clock for it or the job dies during startup.
- **The wall-clock limit will kill a long eval run.** The harness **must checkpoint and
  resume** (ADR-0003). An eval suite that cannot survive a job timeout silently stops
  being run, which is worse than not having one.
- **Queue time is not run time.** A 20-minute job may wait hours. Do not put anything with
  a deadline behind a scheduler you do not control.
- **The endpoint is stale the moment the job ends.** A gateway health probe against a dead
  allocation should report the model as unavailable and exercise the fallback chain —
  which is a free failure drill, so wire it that way deliberately rather than treating it
  as an error.
- **Shared storage is shared.** Dell Unity and the IBM storage hold other people's student
  and faculty projects. Synthetic corpus only. The profile's `classification_ceiling:
  public` enforces this at ingest, but do not let the mechanical check make you casual
  about what you copy there by hand.

---

## Files this directory owes

| File | Purpose |
|---|---|
| `vllm.def` | Apptainer definition for the vLLM server image |
| `vllm-server.sbatch` | Allocation, server start, endpoint publication, tunnel command |
| `stage-weights.sh` | Populate `$HF_HOME` on scratch from the login node |

None exist yet. They are M1 work, not M0 — M0 is `demo-local` only.
