# ADR-0007 — The sandbox is one hardened service on an internal network, not a container per run

**Status:** Accepted · **Date:** 2026-09-23
**Amends:** `packages/tools/AGENTS.md` §"The sandbox" and `services/AGENTS.md` (one-shot
containers, `--network none`, destroyed after use). Also amends PLAN-M0 task 10 ("the
sandbox image runs a trivial program and is destroyed") and task 12 ("the sandbox has no
interface at all").

---

## Context

The plan was a fresh container for every `code.run`, with `--network none`, destroyed
after use.

That requires some Citadel process to create containers, which means holding the Docker
socket or an equivalent API. The socket gives root on the Docker host; under Docker
Desktop ([ADR-0006](./0006-docker-desktop-launcher-and-per-container-enforcement.md)) it
gives control of the whole VM, including every volume (the private keys, the database
files) and every other container. The process that would hold it is the worker. The
worker runs the agent loop over model output, and it is the "compromised orchestrator"
that the receipt design exists to distrust (`packages/tools/AGENTS.md`). Giving that
process the socket so it can contain the code it asked to run would hand it far more
power than the code it contains.

## Decision

One long-lived `sandbox` service (`services/sandbox`, `ops/compose/docker-compose.yml`):

- **Network.** It sits only on the `sandbox` network, which is `internal: true`: no
  gateway, no route out. It does have an interface, because work reaches it over HTTP.
  That interface leads only to the API and worker containers, and their rulesets refuse
  any connection it tries to open (below).
- **Container limits.** It runs as uid 10002 with a read-only root filesystem and a
  size-capped tmpfs at `/tmp`. It has `cap_drop: ALL`, `no-new-privileges`, and PID,
  memory and CPU limits. No host mounts.
- **Keys.** It holds only the receipt **public** key.
- **Receipt check.** Before anything runs, it recomputes the digest of the source it
  actually received and verifies the receipt against it. The receipt must be signed, not
  expired, for this operation, and unused (`citadel_tools.sandbox.run_verified`).
- **Each run.** Every program runs as its own process in a fresh directory. It gets a
  minimal environment, rlimits, and a wall-clock timeout that kills its whole process
  group. An audit hook refuses sockets, subprocesses, foreign native libraries and file
  access outside the run directory. That hook is defence in depth, not the boundary.
  The directory is deleted after the run.

On a machine without containers, `scripts/run.sh` uses the same `run_verified` in-process
(`LocalSandboxRunner`). Every result from that path labels itself `kind: process`, and the
sovereignty panel shows it.

## What this gives up

- **Runs share a container.** A program that escaped the in-process guard could leave a
  file in `/tmp` or a stray process for a later run to find; a container per run would
  not allow that. A CPython audit hook is not a security boundary. What limits the damage:
  - each run directory is deleted afterwards, and the process group is killed on timeout;
  - `/tmp` is a capped tmpfs and the root filesystem is read-only;
  - there is nothing of value inside: no private key, no route to the database, and no
    documents except those sent for the run in question.
- **The sandbox has an interface.** "No interface at all" becomes "an interface on an
  internal network with no route out". Target E is about data leaving the premises, and
  that claim is unchanged: nothing on the sandbox network can reach anywhere else.
- **An escaped program still shares a network with the API and the worker**, so it must
  not be able to call them. This matters more than it sounds, because demonstration
  sign-in has no password (ADR-0001 §Q7): anything that can reach `/api/auth/session` can
  act as a seeded identity. So the API and worker rulesets refuse every **new inbound**
  connection from the sandbox network (`CITADEL_UNTRUSTED_NETWORKS`). The sandbox can
  answer the calls they make to it and can open none of its own. This was checked in a
  scratch network namespace: from a sandbox-network address the API port answered "no
  route to host"; from the data network and loopback it connected.

## Revisit when

- **Per-run isolation becomes available without giving the orchestrator the Docker
  socket.** Two ways that could happen: gVisor (`runsc`) as the sandbox's runtime, or a
  small sandbox-manager that alone holds a socket proxy allowing only create, start and
  remove of one image with fixed options.
- **The threat model changes.** ADR-0001 §Q6 is still unconfirmed. If it is confirmed to
  include hostile code that must be assumed to escape CPython, the shared-container
  weakness above stops being acceptable.
