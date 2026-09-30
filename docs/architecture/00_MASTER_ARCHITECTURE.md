# Sovereign Agentic AI Workbench
## Detailed Block-Level Design & Integration Specification

### 1. System goal

The workbench is a sovereign, on-premise, air-gapped agentic AI platform for confidential industrial, PSU, and defence-linked knowledge work.

The canonical runtime path is:

```text
CLI
  ↓
Query Router + Model Router
  ↓
Orchestrator
  ↓
Control Plane
  ├──────────────→ Execution Plane / Agent Sandbox
  ├──────────────→ Data Plane / Shared State & Memory
  └──────────────→ Network Layer / Egress Enforcement
                         ↓
                     Internet
```

Observability is cross-cutting and receives events from every major block.

```text
                    ┌──────────────────────────────────┐
                    │       OBSERVABILITY PLANE        │
                    │ traces · metrics · audit · eval │
                    └──────────────────────────────────┘
                         ↑      ↑       ↑      ↑
                         │      │       │      │
CLI → ROUTERS → ORCHESTRATOR → CONTROL → EXECUTION / DATA
                                       │
                                       └→ NETWORK
```

### 2. Architectural principle

The most important rule is:

> **Agents do not get direct authority. They request capabilities; the control plane decides whether those capabilities may be used.**

That means an agent cannot directly:
- read arbitrary files
- query arbitrary databases
- call an arbitrary model
- access the host filesystem
- access the network
- publish an artifact
- change another agent's state

All such actions become policy-checked operations.

### 3. Core components

| Block | Primary responsibility | Suggested implementation |
|---|---|---|
| CLI | Human task/query interface | Python Typer/Textual |
| Query Router | Classify and normalize requests | Python/FastAPI |
| Model Router | Select approved local model | Python service + capability registry |
| Orchestrator | Plan, dispatch, observe, revise | LangGraph-style state machine/custom workflow engine |
| Control Plane | Identity, policy, approvals, permissions | FastAPI + OPA-style policy service + Postgres |
| Execution Plane | Run isolated agents/tools | Docker/containerd |
| Data Plane | State, memory, RAG, artifacts | Postgres + Redis + Qdrant + MinIO/object store |
| Network Layer | Default-deny egress | nftables/firewall + gateway |
| Observability | Trace, audit, metrics, evaluation | OpenTelemetry-compatible stack |
| Local Models | Local inference | vLLM/Ollama |
| Tool protocol | Controlled capability interface | MCP |

### 4. Universal request envelope

Every request crossing a service boundary should have a common envelope.

```json
{
  "request_id": "req_123",
  "task_id": "task_456",
  "user_id": "user_789",
  "session_id": "sess_001",
  "classification": "CONFIDENTIAL",
  "intent": "TASK",
  "objective": "Prepare an inspection report",
  "constraints": [],
  "requested_tools": ["internal_search", "ocr", "docx_writer"],
  "requested_model_capabilities": ["vision", "reasoning"],
  "policy_context": {
    "department": "maintenance",
    "clearance": "CONFIDENTIAL"
  },
  "timestamp": "2026-09-03T00:00:00Z"
}
```

### 5. Universal trace identifiers

Every action should be reconstructable using:

```text
request_id
task_id
run_id
agent_id
tool_call_id
model_call_id
artifact_id
event_id
policy_decision_id
parent_trace_id
```

### 6. Universal event format

```json
{
  "event_id": "evt_123",
  "timestamp": "2026-09-03T00:00:00Z",
  "task_id": "task_456",
  "actor_type": "agent",
  "actor_id": "agent_2",
  "event_type": "TOOL_CALL",
  "target": "internal_search",
  "input_hash": "sha256:...",
  "output_hash": "sha256:...",
  "policy_decision_id": "pol_321",
  "result": "ALLOW",
  "parent_event_id": "evt_122"
}
```

### 7. End-to-end golden path

Example: engineer submits a confidential request to produce a maintenance approval note from internal reports and scanned records.

```text
1. CLI accepts /task
2. Query Router classifies:
      task + document + retrieval + vision + reasoning
3. Model Router selects:
      vision model + reasoning model + embedding model
4. Orchestrator generates a plan
5. Control Plane checks:
      identity
      classification
      tool permissions
      data permissions
6. Execution Plane starts isolated agent containers
7. Agents retrieve approved internal documents
8. Vision/OCR extracts data from scans
9. Agent synthesizes findings
10. Shared state stores decisions, evidence and checkpoints
11. Deliverable builder creates DOCX
12. Verifier checks:
      citations
      file integrity
      policy compliance
13. Human approval gate pauses the task
14. Approver accepts/rejects
15. Approved artifact becomes immutable "Released"
16. Observability dashboard shows entire trace
17. Network telemetry proves there was no unauthorized egress
```

### 8. State machine

```text
CREATED
  ↓
PLANNING
  ↓
WAITING_FOR_POLICY
  ↓
RUNNING
  ↓
WAITING_FOR_APPROVAL ──→ REJECTED ──→ REVISION
  ↓
VERIFIED
  ↓
APPROVED
  ↓
RELEASED

At any point:
RUNNING → FAILED → RECOVERING → RUNNING
RUNNING → PAUSED → RUNNING
```

### 9. Security model

Use defense in depth:

```text
Identity
  ↓
Policy decision
  ↓
Capability grant
  ↓
Sandbox isolation
  ↓
Filesystem restrictions
  ↓
Data ACLs
  ↓
Network allow-list
  ↓
Immutable audit event
```

### 10. MVP boundary

For the first prototype, do not build everything at production scale.

Implement:

```text
CLI
→ Router
→ Orchestrator
→ Policy Check
→ One sandboxed agent
→ Local RAG
→ One document generator
→ Approval gate
→ Audit log
→ Network monitor
```

Then demonstrate the complete scan-to-approved-document flow before adding multiple agents, multiple models, complex ABAC, or large-scale scheduling.
