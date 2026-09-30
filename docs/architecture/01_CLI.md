# Block 01 — CLI

## Role

The CLI is the primary human-facing interface shown in the concise architecture.

It should feel like a task console rather than a normal chatbot.

Example:

```text
>>> /task report_on_lathe
>>> /status task_104
>>> /approve task_104
>>> /resume task_104
>>> /artifacts task_104
>>> /trace task_104
```

## Responsibilities

1. Accept human intent.
2. Convert slash commands into structured requests.
3. Display task progress.
4. Display approval requests.
5. Query existing state, memory and activity.
6. Never bypass the router/control plane.

## Suggested technology

- Python
- Typer for commands
- Rich for formatted terminal output
- Textual later if a TUI is desired
- WebSocket/SSE for streaming activity

## Internal structure

```text
cli/
├── main.py
├── commands/
│   ├── task.py
│   ├── status.py
│   ├── approve.py
│   ├── artifacts.py
│   └── trace.py
├── client/
│   └── api_client.py
└── render/
    ├── task_view.py
    └── activity_view.py
```

## Command contract

### /task

```text
/task <task_name>

Optional flags:
--objective
--classification
--department
--deadline
--model-policy
```

The CLI sends:

```json
{
  "intent": "TASK",
  "command": "/task",
  "objective": "...",
  "classification": "CONFIDENTIAL",
  "user_id": "...",
  "session_id": "..."
}
```

## /status

Reads only task state exposed by the control/data APIs.

```text
Task: task_104
Phase: RUNNING
Current agent: maintenance_analyst
Progress: 6/9 steps
Waiting on: retrieval
Policy status: ALLOWED
```

## /approve

Must never directly change artifact state.

It sends an approval request:

```json
{
  "intent": "APPROVAL",
  "task_id": "task_104",
  "decision": "APPROVE",
  "approver_id": "user_42",
  "comment": "Reviewed and accepted"
}
```

Control Plane validates whether that user is authorized to approve.

## CLI → Query Router

Use authenticated HTTP or local IPC.

```text
CLI
  POST /v1/requests
      ↓
Query Router
```

Recommended header set:

```text
Authorization
X-Request-ID
X-Session-ID
X-Client-Version
```

## Important rule

The CLI never receives:
- database credentials
- model server credentials
- filesystem root access
- Docker socket
- unrestricted MCP credentials

## Failure handling

If the backend is unavailable:

```text
CLI → show "Workbench unavailable"
CLI → do not execute locally as fallback
```

That prevents accidental policy bypass.

## Prototype acceptance test

A successful prototype should support:

```text
/task create maintenance report
/status
/approve
/artifacts
/trace
```

while producing a complete backend audit trail for each command.
