# ADR-0004 — The demonstration runs on one box

**Status:** Accepted · **Date:** 2026-09-21
**Supersedes:** ADR-0001 §Q2
**Follows from:** ADR-0003

---

## Context

ADR-0001 §Q2 was answered "two boxes", and the reasoning was sound at the time: separating
the inference server from the application box removes GPU residency contention, which on an
8 GB card is the dominant constraint.

Two things have changed since.

1. **ADR-0003 removed the cluster as a deployment target.** It is a measuring instrument,
   never sovereign, never on the demonstration path.
2. **There is no second machine.** The RTX 5060 workstation is the only hardware that will
   be in the room, and the project has to work on it.

So the premise for "two boxes" has evaporated. The honest statement is the third option
that was on the table when §Q2 was asked and was not chosen: *one box at demonstration
time, the split preserved as a deployment option.*

## Decision

**`demo-local` is a single machine.** Ollama, Postgres, the API, workers, the sandbox, OCR,
embedding and reranking all run on the RTX 5060 workstation.

The two-box split survives as a Compose override, for a future deployment where the
customer organisation has a separate GPU server. It is not the default and it is not
exercised at the demonstration.

### Why this costs almost nothing to change

ADR-0001 §Q2's mitigation was written for exactly this: the Model Gateway holds the
inference endpoint in the registry, and a structural test forbids anything outside the
gateway from referencing an inference host. One box and two boxes were always meant to
differ by a URL.

They do. The endpoint becomes local and nothing else moves.

## What we lose

The CPU/GPU isolation that motivated §Q2 in the first place. OCR, embedding, reranking,
Postgres, the sandbox and Ollama now share one machine.

The **GPU** side is unaffected — Ollama still owns the card, and the resident-set budget in
ADR-0002 is unchanged. What is now contended is **system RAM and CPU**, and Ollama needs
host RAM of its own to stage models before they reach VRAM.

**So `gpu_admission` is no longer the only cap that matters.** ADR-0001 §Q12 said task
concurrency is unbounded and only GPU admission is bounded. On one box that is no longer
safe: a bulk corpus ingest running OCR across every core will starve the API and make the
UI look hung during a demonstration, which is the worst possible moment.

Add a second, independent cap: **`cpu_admission`**, bounding concurrent ingestion and OCR.
It is the same mechanism as `gpu_admission`, sized from the profile registry, and it keeps
§Q12's principle intact — concurrency is bounded at the scarce resource, and on one box
there are two scarce resources rather than one.

## What we gain, which is more than we lose

**Target E becomes dramatically more convincing.**

On two boxes, "nothing leaves the premises" is an argument supported by firewall rules and
a telemetry panel. An observer has to trust the instrumentation.

On one box, it is a physical demonstration: **unplug the network cable, turn off the wifi,
and run the entire flow** — ingest a scanned report, route between models, execute code in
the sandbox, generate the .docx, approve and release it. Nothing degrades, because nothing
was ever reaching out.

That is the most convincing form the sovereign claim can take, it requires no instrument to
be believed, and two boxes made it impossible. The sovereignty panel and the deliberate
probe stay — they are what makes the claim auditable rather than theatrical — but the cable
is what makes it land.

There is also one nftables ruleset to write and audit instead of two.

## ⚠️ Open: what OS does the demonstration machine run? — **resolved, see [ADR-0005](./0005-wsl2-execution-environment.md)**

This now matters more than it did, and it is a question rather than a decision.

The egress enforcement in ADR-0001 §Q2 and `packages/sovereignty/AGENTS.md` assumes
**nftables**, which is Linux. If the RTX 5060 workstation runs Windows with Docker Desktop,
that ruleset does not apply to the host, and the enforcement half of the sovereignty
subsystem needs a different mechanism — which would weaken the strongest part of the demo.

| If the machine is… | Then |
|---|---|
| **Linux** | Everything above holds as written. This is the preferred answer. |
| **Windows + WSL2** | Run the whole stack inside WSL2, where nftables works, and treat the WSL2 VM as the box. The cable demonstration still works. Verify that GPU passthrough to WSL2 is healthy on sm_120 early — it is a known source of trouble on Blackwell. |
| **Windows + Docker Desktop, stack split across host and containers** | Weakest option. Enforcement falls back to Docker internal networks plus a monitoring sidecar, and the host itself is outside the ruleset. Avoid. |

**Recommendation: Linux on the demonstration machine, or the whole stack inside WSL2.**
Decide this before M0 task 12, because it determines what `ops/nftables/` contains.

## Consequences

- `registry/profiles.yaml` — `demo-local` endpoint becomes local; `cpu_admission` added.
- `packages/runtime` — a second admission semaphore, and the live task view distinguishes
  "waiting on GPU" from "waiting on CPU" so a queued task still reads as queued.
- `ops/compose/` gains a two-box override that is documented and not exercised.
- The demonstration script gains a step: disconnect the network before starting.
- ADR-0001 §Q6's threat model loses the app-box ↔ GPU-box link that ADR-0002 added. One
  box, one perimeter. The `hpc-eval` link from ADR-0003 is unaffected and is still handled
  by ensuring nothing worth protecting crosses it.

## Revisit when

- The customer organisation provides a separate GPU server, at which point the override
  becomes the deployment and this ADR's §Q2 reversal reverses back — which should require
  only registry and Compose files. If it requires more, the seam was wrong.
