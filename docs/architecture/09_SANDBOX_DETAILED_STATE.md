# Block 09 — Sandbox / Shared State Model

## Role

The concise diagram contains a Sandbox section with:

- Task
- Shared State
- Agent State
- Artifacts
- Resources
- Event / Execution History

This file turns that conceptual box into an implementable design.

## Task

```json
{
  "task_id": "task_104",
  "objective": "...",
  "constraints": ["use sector-1 reports"],
  "status": "RUNNING",
  "current_phase": "ANALYSIS"
}
```

## Shared State

```json
{
  "shared_plan": {},
  "decisions": [],
  "discovered_facts": [],
  "assumptions": [],
  "state_version": 17
}
```

Only orchestrator-approved mutations should update shared state.

## Agent State

Every agent gets isolated state:

```json
{
  "agent_id": "agent_2",
  "current_goal": "Analyze inspection records",
  "progress": 0.65,
  "working_memory": [],
  "last_checkpoint": "chk_19"
}
```

## Artifacts

```json
{
  "artifact_id": "artifact_22",
  "type": "DOCX",
  "status": "VERIFIED",
  "version": 4,
  "sha256": "...",
  "sources": ["manual_17", "report_52"]
}
```

## Resources

These are capabilities available to the task:

```text
TOOLS
  internal_search
  OCR
  spreadsheet
  code_execution
  docx_writer

MCP SERVERS
  internal_db
  CAD analyzer

MODELS
  reasoner_01
  vision_01
  embedding_01
```

A resource entry is not automatically permission to use it.

## Event history

Capture state transitions:

```text
TaskCreated
PlanCreated
AgentStarted
PolicyChecked
ToolInvoked
ObservationRecorded
StateCommitted
ArtifactCreated
ApprovalRequested
ApprovalGranted
ArtifactReleased
```

## Crash recovery

The sandbox itself is disposable.

Persistent state must live outside it.

Recovery:

```text
container dies
 ↓
read last checkpoint
 ↓
restore task state
 ↓
recreate agent
 ↓
resume next unfinished step
```

## Concurrency

Recommended initial strategy:

- optimistic locking for shared state
- per-artifact ownership
- append-only events
- deterministic merge functions for known state types

Avoid distributed locking everywhere during MVP.
