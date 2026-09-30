# Block 05 — Control Plane / Policy Engine

## Role

The Control Plane is the security and governance choke point.

Its job is to answer:

```text
WHO is acting?
WHAT are they trying to do?
WHAT data are they accessing?
WHICH tool/model are they requesting?
IS the action allowed?
DOES it require approval?
```

## Main services

```text
┌────────────────────────────────────┐
│           CONTROL PLANE            │
├───────────────┬────────────────────┤
│ Identity      │ Policy Engine      │
│ Service       │                    │
├───────────────┼────────────────────┤
│ Capability    │ Approval Service   │
│ Registry      │                    │
├───────────────┼────────────────────┤
│ Audit/Event   │ Secret/Key Service │
│ Service       │                    │
└───────────────┴────────────────────┘
```

## Policy inputs

A policy decision should consider:

```text
subject
role
department
clearance
task classification
resource classification
requested action
tool risk
model approval status
environment
time/window
approval requirements
```

## RBAC + ABAC

Example RBAC:

```text
Engineer
Reviewer
Approver
Administrator
Auditor
```

ABAC can then refine access:

```text
department == "maintenance"
AND resource.department == "maintenance"
AND classification <= user.clearance
```

## Capability-based permissions

Instead of broad permissions:

```text
"agent can access database"
```

issue narrow capabilities:

```text
"agent can read table maintenance_reports
 where sector=1
 and classification <= CONFIDENTIAL"
```

## Policy decision example

```json
{
  "subject": "agent_2",
  "action": "READ",
  "resource": "maintenance_reports",
  "classification": "CONFIDENTIAL",
  "scope": {
    "sector": "1"
  }
}
```

Result:

```json
{
  "decision": "ALLOW",
  "policy_id": "policy_17",
  "expires_at": "...",
  "conditions": [
    "read_only",
    "sector=1"
  ]
}
```

## Human approval

Approval gates should be state transitions.

```text
DRAFT
 ↓
REVIEW_REQUIRED
 ↓
APPROVED
 ↓
RELEASED
```

The approver decision should contain:

```json
{
  "artifact_id": "artifact_22",
  "approver_id": "user_42",
  "decision": "APPROVE",
  "comment": "...",
  "timestamp": "...",
  "previous_hash": "..."
}
```

## Audit model

Log both allowed and denied actions.

Important event types:

```text
LOGIN
TASK_CREATED
POLICY_CHECK
POLICY_DENIED
MODEL_SELECTED
RETRIEVAL
TOOL_CALL
FILE_READ
FILE_WRITE
AGENT_STARTED
AGENT_FAILED
APPROVAL_REQUESTED
APPROVED
RELEASED
NETWORK_BLOCKED
NETWORK_ALLOWED
```

## Secrets

Do not place secrets in prompts, source code, or agent state.

Agents receive short-lived capability-scoped credentials where necessary.

## Emergency controls

Provide:

```text
KILL TASK
KILL AGENT
DISABLE TOOL
DISABLE MODEL
GLOBAL NETWORK BLOCK
QUARANTINE ARTIFACT
```

## Implementation

Suggested:

- FastAPI
- Postgres
- OPA-style policy engine or equivalent
- internal identity provider/LDAP/AD integration
- append-only event table
- cryptographic hashing for tamper evidence

The control plane should remain independent of the LLM framework.
