# Block 04 — Execution Plane / Agent Sandbox

## Role

This is where agents actually execute.

The key architecture statement is:

> **The execution plane can act, but it cannot decide what it is allowed to act on.**

Permissions come from the control plane.

## Container layout

Per task:

```text
task-104/
├── agent-a/
│   ├── /workspace
│   ├── /inputs
│   └── /outputs
├── agent-b/
│   ├── /workspace
│   └── /outputs
└── shared/
    ├── approved_inputs
    └── shared_artifacts
```

## Isolation controls

At minimum:

- separate container per agent/task
- CPU limits
- memory limits
- read-only base image
- limited writable mounts
- no Docker socket
- no host filesystem mount
- non-root user
- dropped Linux capabilities
- default-deny network

Where practical:

- seccomp
- AppArmor
- read-only root filesystem
- process count limit

## Tool gateway

Agents should invoke:

```text
Agent
  ↓
MCP/tool gateway
  ↓
Control Plane
  ↓
Tool
```

Not:

```text
Agent → arbitrary subprocess
Agent → database
Agent → Internet
```

## Tool definitions

Each tool should declare:

```json
{
  "tool_id": "docx_writer",
  "risk": "LOW",
  "input_types": ["artifact"],
  "output_types": ["docx"],
  "requires_approval": false,
  "network_required": false,
  "allowed_classifications": [
    "INTERNAL",
    "CONFIDENTIAL"
  ]
}
```

## Sandboxed code execution

For Python code:

```text
Agent submits code
      ↓
Policy check
      ↓
Create one-shot container
      ↓
Mount only task inputs
      ↓
Execute
      ↓
Capture stdout/stderr/artifacts
      ↓
Hash output
      ↓
Destroy container
```

Do not execute model-generated code in the orchestrator process itself.

## OCR / vision

OCR jobs should also execute within a controlled task runtime.

```text
scan
 ↓
sandbox vision process
 ↓
extracted text + bounding boxes
 ↓
artifact/evidence store
```

## Artifact boundary

An agent's temporary output is not automatically a final artifact.

States:

```text
TEMP
 ↓
CANDIDATE
 ↓
VERIFIED
 ↓
APPROVED
 ↓
RELEASED
```

## Failure recovery

If an agent crashes:

```text
container failed
 ↓
capture logs
 ↓
emit AGENT_FAILED event
 ↓
persist state
 ↓
retry or reassign
```

The sandbox should be disposable. Important state lives outside it.
