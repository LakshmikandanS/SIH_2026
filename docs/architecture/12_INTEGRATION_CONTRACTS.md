# Block 12 — Integration Contracts

## 1. CLI → Query Router

```http
POST /v1/requests
Content-Type: application/json
```

```json
{
  "request_id": "req_1",
  "user_id": "u_1",
  "session_id": "s_1",
  "intent_hint": "TASK",
  "objective": "Prepare inspection approval note",
  "classification": "CONFIDENTIAL"
}
```

Response:

```json
{
  "task_id": "task_1",
  "route": "TASK",
  "status": "ACCEPTED"
}
```

## 2. Query Router → Model Router

```json
{
  "task_id": "task_1",
  "required_capabilities": [
    "retrieval",
    "vision",
    "reasoning"
  ],
  "classification": "CONFIDENTIAL"
}
```

Response:

```json
{
  "selected_models": {
    "vision": "vision_01",
    "reasoning": "reasoner_02",
    "embedding": "embed_01"
  }
}
```

## 3. Orchestrator → Control Plane

Every privileged operation:

```json
{
  "task_id": "task_1",
  "agent_id": "agent_1",
  "action": "RETRIEVE",
  "resource": "maintenance_reports",
  "scope": {
    "sector": "1"
  }
}
```

Response:

```json
{
  "decision": "ALLOW",
  "policy_decision_id": "pol_1",
  "capability_token": "opaque-short-lived-token"
}
```

## 4. Orchestrator → Sandbox

Sandbox creation:

```json
{
  "task_id": "task_1",
  "agent_id": "agent_1",
  "image": "agent-runtime:1.0",
  "resources": {
    "cpu": 4,
    "memory_gb": 8,
    "gpu": true
  },
  "mounts": [
    {
      "name": "approved_inputs",
      "mode": "ro"
    }
  ]
}
```

## 5. Agent → Tool Gateway

```json
{
  "task_id": "task_1",
  "agent_id": "agent_1",
  "tool_id": "internal_search",
  "arguments": {
    "query": "lathe repair history"
  },
  "capability_token": "..."
}
```

## 6. Tool Gateway → Data Plane

The tool gateway should enforce:
- authorization context
- ACL filters
- scope
- read/write operation
- audit event

## 7. Data Plane → Orchestrator

State update:

```json
{
  "task_id": "task_1",
  "expected_version": 12,
  "patch": {
    "decisions": [
      "Repair cost increased after bearing replacement"
    ]
  }
}
```

Response:

```json
{
  "status": "COMMITTED",
  "new_version": 13
}
```

or:

```json
{
  "status": "CONFLICT",
  "current_version": 14
}
```

## 8. Observability

All services emit:

```text
trace_id
span_id
task_id
actor_id
event_type
input/output hashes
policy decision
timestamp
```

## 9. Network

No application component should directly decide that an external route is allowed.

Network permission is a separate enforcement layer.

## 10. Integration rule

The architecture should remain valid even when:
- model vendors change
- vector DB changes
- orchestration library changes
- hardware changes
- UI changes

The contracts are the stable architecture.
