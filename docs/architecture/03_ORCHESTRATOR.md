# Block 03 — Orchestrator

## Role

The orchestrator is the workflow brain.

Its loop is:

```text
PLAN
 ↓
ACT
 ↓
OBSERVE
 ↓
REVISE
 ↓
ACT ...
```

It should manage work, not directly perform privileged work.

## Main responsibilities

- create plans
- create agent runs
- dispatch tools
- collect observations
- update state
- detect failure
- request policy checks
- trigger approval gates
- resume from checkpoints

## Recommended state model

```python
TaskState = {
    "task_id": "...",
    "objective": "...",
    "phase": "RUNNING",
    "shared_plan": [...],
    "completed_steps": [...],
    "active_agents": [...],
    "assumptions": [...],
    "decisions": [...],
    "evidence": [...],
    "artifacts": [...],
    "pending_approvals": [...]
}
```

## Plan object

```json
{
  "plan_id": "plan_01",
  "version": 3,
  "steps": [
    {
      "step_id": "s1",
      "action": "retrieve_reports",
      "required_capabilities": ["internal_search"]
    },
    {
      "step_id": "s2",
      "action": "analyze_scans",
      "required_capabilities": ["vision"]
    },
    {
      "step_id": "s3",
      "action": "draft_note",
      "required_capabilities": ["reasoning", "docx"]
    }
  ]
}
```

## Dispatch sequence

```text
Orchestrator
    ↓
Create step
    ↓
Ask Control Plane for capability grant
    ↓
Grant?
 ┌──┴───┐
yes    no
 ↓      ↓
spawn   PAUSE / escalate
agent
 ↓
tool call
 ↓
observation
 ↓
persist event/state
 ↓
continue or revise
```

## Multi-agent pattern

Example:

```text
                 Supervisor
                /     |      \
               /      |       \
        Retrieval   Vision   Report
          Agent      Agent     Agent
              \       |       /
               \      |      /
                Shared Evidence
```

Agents should not write to shared state arbitrarily.

Use controlled mutation:

```text
read_state(version=17)
make_change
compare version
commit(version=17)
```

If version changed:

```text
CONFLICT
→ re-read
→ merge/recalculate
```

## Checkpointing

Checkpoint at:

- after plan creation
- after retrieval
- after each expensive model call
- before artifact publication
- before human approval

A crash should allow:

```text
resume(task_id)
```

without re-running completed expensive steps.

## Retry policy

Not every error should retry.

```text
Transient model timeout → retry
Temporary DB error → retry
Policy denial → no retry
Invalid user approval → no retry
Repeated tool failure → escalate
```

## Orchestrator interfaces

```text
POST /tasks
POST /tasks/{id}/runs
POST /tasks/{id}/resume
POST /tasks/{id}/pause
GET  /tasks/{id}
GET  /tasks/{id}/activity
```

## Recommended implementation

For the prototype:

- Python
- LangGraph-style state machine
- explicit typed states
- Postgres persistence
- Redis optional for queues/events

Do not let framework abstractions hide policy decisions. Policy must remain an explicit control-plane call.
