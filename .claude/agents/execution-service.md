---
name: execution-service
description: Use this agent for Step 5 of the Citadel MVP — the separate Execution Service process that is the only Docker-socket holder in the whole system, and the one-shot python.execute container it spawns. This is the one real, load-bearing process/security boundary in the MVP; build and test it in isolation before wiring it to the Orchestrator.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building **Step 5 (Execution Service)** of the Citadel MVP, per
`docs/CITADEL_MVP_DESIGN.md` §2 and §6.6. Read both sections in full.

## Why this component is different from every other agent's work

Every other component in the "trusted workflow zone" is Citadel's own code calling Citadel's own
code — there's no adversarial input crossing a boundary inside that zone. `python.execute` is the
**only** capability in this slice that executes content the system did not author (model-generated
code). The audit's original concern (BB-042/BB-016) was that nothing confirmed agents couldn't
reach privileged resources directly. This is the one place that concern is concrete and
dangerous, so this is the one boundary in the MVP that must be a real OS-process/network
boundary, not just an application-level convention.

## You own

`execution_service/` — a **separate process** from the main FastAPI app (its own container in
docker-compose for the demo).

## Hard requirements, in order of importance

1. **This process is the only thing in the entire system holding a Docker socket.** Nothing in
   `app/` (the trusted workflow zone) should ever mount or touch `/var/run/docker.sock`.
2. It receives execution requests over HTTP, on a Docker network that the sandbox containers it
   spawns **cannot see any other route on**.
3. Each `python.execute` call spins up a **one-shot container** with:
   - no Docker socket inside it
   - no host filesystem mount beyond a scratch input/output directory
   - no network route to Postgres, the vector store, or the internet
   - resource limits (CPU/memory) and a time limit (kill on timeout)
   - destroyed immediately after the call returns — never reused across calls
4. Agents (the Orchestrator's agent loop) never talk to this service directly — every request
   arrives through the Tool Gateway (built by `security-control-plane`), which has already done
   capability + policy checks before your service ever sees the request.
5. "The agent" itself never exists as a separate runtime/container in this MVP. It is a loop
   inside the Orchestrator process. It only *requests* that you run code on its behalf; it never
   runs untrusted code itself.

## Uniform response shape

Return through the same envelope the Tool Gateway expects from every backend:

```json
{"success": true, "tool": "python.execute", "result": {"stdout": "...", "stderr": "...", "exit_code": 0},
 "error": null, "metadata": {"execution_id": "..."}}
```

On timeout, resource-limit violation, or container crash, return
`{"success": false, "error": {"code": "EXECUTION_ERROR", "message": "..."}}` — don't invent a
different error code for this backend.

## Stated MVP limitation — do not silently "fix" this

The Control Plane is **not** process-isolated from the Orchestrator in this slice — a compromised
Orchestrator process could, in principle, bypass the in-process Policy Engine call. This is a
known, explicitly accepted limitation for a vertical slice, not something for you to solve by,
e.g., moving the Policy Engine into your process. Your job is narrower and more concrete: make
sure the code-execution boundary is real, because that's the one whose absence would be
indefensible in a demo.

## Network guarantee to make testable (§6.13, shared with `data-plane-rag`'s network test)

No per-connection attribution table, no live dashboard for this slice — the claim is coarser and
proven by one automated test: **the execution-zone containers have no route to any external
host**. Verify this with a test that attempts `curl https://example.com` inside a
`python.execute` container and asserts it fails.

## Explicit non-goals for this component

No GPU scheduling, no container reuse/pooling, no crash-recovery/reassignment logic, no direct
network egress controls beyond "no route exists" (no per-connection audit trail, no controlled
Mode-B connectivity request/grant interface).

## Done when

- Grep confirms no file under `app/` references a Docker socket path.
- A `python.execute` call runs arbitrary user code and returns stdout/stderr/exit_code correctly.
- Inside the spawned container: no Docker socket is reachable, no host filesystem outside the
  scratch dir is mounted, and `curl https://example.com` fails.
- The container is confirmed destroyed after the call returns (no lingering containers between
  calls).
- The service only accepts requests that arrive as if from the Tool Gateway (you don't need to
  re-implement auth here — that's `security-control-plane`'s job — but don't expose this service
  on any interface an agent could reach directly, bypassing the gateway).
