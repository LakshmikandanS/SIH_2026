---
name: orchestrator
description: Use this agent for Step 7 of the Citadel MVP — task intake (the Query Router role, folded in here for the MVP), plan generation via the Model Router, the deterministic agent execution loop, and the one scoped plan-revision case. Requires foundation-schema, security-control-plane, execution-service, and data-plane-rag to already exist, since this agent wires them together for real.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building **Step 7 (Core orchestration)** of the Citadel MVP, per
`docs/CITADEL_MVP_DESIGN.md` §5, §6.2, and §6.3. Read all three in full.

## Mission

This is where every previously-isolated piece gets wired together for real: task intake → a real
planning call → a real agent loop making real tool calls through the Tool Gateway you already
built and tested with a fake tool → an artifact request → the one scoped revision path.

## You own

`app/orchestrator/` (includes the Query Router's task-intake role — it is not a separate service
in this MVP) and `app/model_router/`.

## Task intake — the Query Router role (§6.1, §6.2)

`POST /login {username, password}` (built by `security-control-plane`) gives the CLI a Bearer
JWT. Your endpoint accepts:

```
/task "Using the available internal maintenance documents, identify the recent maintenance
history of Pump P-101 and generate a short maintenance summary report." --classification CONFIDENTIAL
```

Classification is **rule-based, not ML**: the slash command `/task` implies class `TASK`; the
`--classification` flag is user-declared and taken as authoritative, never inferred or
overridden. You (the Query Router role) create the `Task` row and return `task_id` to the CLI
**immediately** — this row-creation step belongs to you, but from this point on **you own all
task state**; nothing else in the system exposes a competing `/tasks/{id}` or
`/tasks/{id}/trace` endpoint.

Canonical handoff into your own orchestration logic (synchronous — the demo scenario runs in
seconds, no async job queue needed):

```json
// POST /internal/orchestrate
{
  "task_id": "T123", "user_id": "U123", "classification": "CONFIDENTIAL",
  "task_type": "DOCUMENT_ANALYSIS",
  "requirements": {"needs_rag": true, "needs_document_generation": true}
}
```

Error response shape: `{"error": {"code": "TASK_ALREADY_RUNNING" | "INVALID_REQUIREMENTS", "message": "..."}}`.

## Model Router (§6.3) — one canonical schema for every model call in this slice

```json
// Request
{"task_id": "T123", "required_capabilities": ["reasoning"], "classification": "CONFIDENTIAL"}

// Response — the ONE shape. Any older single-model shape elsewhere in docs/architecture/ is
// retired for this slice; do not build both.
{
  "selected_models": {"reasoning": "qwen3-local", "embedding": "bge-base-local"},
  "routing_reason": "local_confidential_reasoning",
  "fallback_chain": []
}
```

Selection logic is deliberately trivial — a **static lookup table**, not a scoring formula:
classification requiring local-only → `reasoning: qwen3-local`; embedding is always needed →
`embedding: bge-base-local`. No health-check, no fallback population (`fallback_chain` always
`[]`) — if the single local model call fails outright, the task transitions to `FAILED`. Add one
field to the model manifest: `"max_classification": "CONFIDENTIAL"` — a task whose classification
exceeds a model's `max_classification` fails routing outright with reason
`MODEL_CLASSIFICATION_INCOMPATIBLE`.

## Plan generation (§5.2)

One structured-output call to the reasoning model, fixed prompt template, response validated
against a JSON Schema:

```json
{
  "plan_id": "P123", "task_id": "T123",
  "steps": [
    {"step_id": "S1", "agent_type": "researcher", "action": "rag.search",
     "arguments": {"query": "Pump P-101 maintenance history"}},
    {"step_id": "S2", "agent_type": "researcher", "action": "python.execute",
     "arguments": {"code": "<computed at runtime from S1's evidence>"}},
    {"step_id": "S3", "agent_type": "writer", "action": "generate_report",
     "arguments": {"template": "maintenance_summary_v1"}}
  ]
}
```

If the model's output fails schema validation, send exactly one repair prompt ("your last output
was invalid JSON against this schema: ..."). A second failure marks the task `FAILED`. Do not
build a general planning algorithm — the plan shape for this scenario is effectively fixed; the
LLM call exists to prove the mechanism, not to demonstrate open-ended planning.

## Agent execution loop (§5.1) — a plain function, not a framework

Executed once per plan step, inside your process:

```
THINK        → given the step's action + current evidence, decide the concrete arguments
ACTION       → emit exactly one structured action: {"action": str, "arguments": {...}}
OBSERVATION  → receive the Tool Gateway's result envelope (success/result or error)
DECISION     → CONTINUE (next step) | RETRY (same step, ≤1 retry) | TERMINATE
```

Termination states: `SUCCESS`, `FAILED`, `MAX_STEPS` (default `6`),
`WAITING_FOR_APPROVAL` (reached after the last plan step succeeds and an artifact is produced).
**The agent never calls a tool directly** — every `ACTION` routes through the Tool Gateway built
by `security-control-plane`, and the agent never sees a capability token it wasn't issued for
that specific step (request one capability from `security-control-plane`'s issuance endpoint
immediately before each step executes).

## Plan revision (§5.3) — scoped to exactly one case

Triggered only when an approver **REJECT**s a `VERIFIED` artifact:

1. Keep all completed steps' evidence and events untouched.
2. Discard only the `generate_report` step's output artifact.
3. Re-run `generate_report` once with the approver's `comment` appended to the prompt.
4. If the second attempt is also rejected, the task ends `FAILED` — no further revision loop.

No general replanning, no step re-ordering, no mid-execution plan change for any other trigger.

## Authoritative state (§6.11)

Only you commit authoritative task state — the agent loop only *proposes* observations, it never
has write credentials to Postgres directly (that's enforced at the network level by
`execution-service` and at the application level by you simply not exposing a generic
state-mutation endpoint). Use the `expected_version` optimistic-versioning check
(`foundation-schema` built the column) for task-status updates: mismatch → `409 CONFLICT`, retry
the read-modify-write once. Because this MVP runs one task at a time sequentially, no semantic
merge of concurrent agent outputs is needed.

## Events you must emit

`TASK_CREATED, PLAN_CREATED, AGENT_STARTED, ACTION_REQUESTED, STATE_COMMITTED` at minimum, plus
whatever the Tool Gateway/Data Plane/Execution Service already emit on their side of each call.

## Explicit non-goals for this component

No multi-agent delegation, no Supervisor pattern (there is exactly one `Agent` row per task —
`agent_type` is a label, not a spawned sub-agent), no checkpoint/resume, no async job queue.

## Done when

- `/task` → plan produces valid, schema-conformant JSON (including the one repair-retry path).
- Every plan step's action reaches the Tool Gateway — never a tool directly (grep confirms no
  direct import of `data_plane`/`execution_service` client code from inside the agent loop).
- Observations feed correctly into the next step's THINK.
- The agent terminates correctly on `SUCCESS`, `FAILED`, and `MAX_STEPS`.
- The one scoped revision case (reject → regenerate once → FAILED on second rejection) works.
