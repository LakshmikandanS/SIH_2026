---
name: cli
description: Use this agent for Step 9 (last) of the Citadel MVP — the citadel command-line client (login, /task, /status, /approve, /trace, admin disable-tool). Only start this once artifact-pipeline is working, since the CLI is what makes the whole system human-drivable for the demo.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building **Step 9 (CLI)** of the Citadel MVP, per `docs/CITADEL_MVP_DESIGN.md` §1 and
§6.1. Read §1 in full — it is the exact script your CLI needs to make possible.

## Mission

Every other agent built an API. This agent makes the whole system drivable by a human, and is
what actually gets demoed. Build it last, once `artifact-pipeline` works, and build it directly
against the three walkthroughs in §1 of the design doc — happy path, denial path, and
emergency-control path. All three must be runnable as commands a person actually types, not just
covered by internal tests.

## You own

`cli/`.

## Commands

- **`citadel login`** — prompts for username/password once, `POST /login`, caches the returned
  session JWT in memory for the process lifetime. No other credential form.
- **`/task "<objective>" --classification CONFIDENTIAL`** — submits a task, prints the returned
  `task_id` immediately.
- **`/status <task_id>`** — shows current Task/Artifact/Approval state (call the Orchestrator's
  `GET /tasks/{id}` — remember, only the Orchestrator serves this, never Control Plane or Data
  Plane).
- **`/approve <approval_id> --decision APPROVED|REJECTED [--comment "..."]`** — calls
  `POST /approvals/{approval_id}/decision`. Do not accept or send an `approver_id` — it's derived
  server-side from the session.
- **`/trace <task_id>`** — prints the full ordered event log via `GET /tasks/{id}/trace`, proving
  the chain end to end. This must clearly show `TOOL_DENIED` and emergency-control events too,
  not only happy-path events — format it so a denial reads as a normal, first-class outcome, not
  as an error dump.
- **`citadel admin disable-tool <tool_name>`** — admin-role-only, calls
  `POST /admin/tools/{tool_name}/disable`.

## The three demo scripts you're actually building toward

**1. Happy path** (§1.1): login → submit the Pump P-101 task → watch it plan, retrieve evidence,
compute, generate a report → `/approve` it → `/trace` shows the full chain ending in
`ARTIFACT_RELEASED`.

**2. Denial path** (§1.2, mandatory, not optional): the same kind of task, but the agent attempts
a `rag.search` for `acl: ["finance"]` documents outside its `department: "maintenance"` scope.
Expected: `POLICY_DENIED`, no data leaves the Data Plane, a `TOOL_DENIED` event is recorded, and
`/trace` shows it identically to a happy-path step — a denial is a normal, observable outcome,
not a crash.

**3. Emergency-control path** (§1.3): `citadel admin disable-tool python.execute`, then a task
that would use it gets `TOOL_DENIED` with reason "tool disabled by administrator," even though
its capability token is still otherwise valid. This proves central, immediate revocation
independent of any already-issued capability.

## Explicit non-goals for this component

No TUI/dashboard beyond plain CLI output, no offline mode, no alternate credential forms (SSO,
API keys) — session JWT via `citadel login` is the only auth path for this slice.

## Done when

All three scripts above run start-to-finish as a human typing commands, and `/trace` after each
one shows a complete, correctly-ordered, hash-chain-intact event log — this is effectively the
final check against the design doc's §11 Definition of Done, so coordinate with the `qa-tester`
agent before calling this finished.
