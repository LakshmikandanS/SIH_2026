# Citadel — Minimum Vertical Slice: MVP Design

**Purpose:** A single, implementation-ready design that closes every black box the vertical slice touches, using the Citadel architecture (`00`–`14`) and the Technical Black-Box Audit (BB-001–BB-047, C-001–C-005) as inputs.
**Status:** Design for implementation — schemas, contracts, and state machines below are meant to be typed in directly, not further debated.
**Non-goal:** This is not a redesign of Citadel and not a re-audit. Every decision below is the *smallest coherent choice* that makes the vertical slice real and defensible, with every simplification stated explicitly rather than left implicit.

---

## 0. Scope

**In scope — the one scenario, end to end:**

> A maintenance engineer asks Citadel to identify Pump P-101's recent maintenance history from internal (CONFIDENTIAL) documents and generate a short summary report, which a human approver then releases.

**Explicitly out of scope for this slice** (deferred, not forgotten — see §7 Closure Matrix for the audit trail):

- Multi-agent orchestration, agent-to-agent delegation, plan revision beyond one trivial case
- GPU scheduling, model health-checking, model fallback chains
- Crash recovery / checkpoint-resume, container-failure reassignment
- A real policy DSL, RBAC/ABAC combination logic, more than 4 static rules
- Network-connection-level attribution, backup/restore, data retention/purge
- Every emergency control except one (`DISABLE TOOL`)
- A production identity provider (LDAP/AD), token revocation lists
- Distributed event ordering — one serializing writer is sufficient at this scale

Nothing above is silently ignored; each maps to a specific BB with a documented reason in §7.

---

## 1. The Vertical Slice, Walked End to End

### 1.1 Happy path

| # | Step | Component | Concrete MVP behavior |
|---|------|-----------|------------------------|
| 1 | User authenticates | CLI + Control Plane (Identity) | Engineer runs `citadel login`, enters username/password once; CLI caches a short-lived session JWT in memory for the process lifetime. |
| 2 | User submits task | CLI → Query Router | `/task "Using the available internal maintenance documents, identify the recent maintenance history of Pump P-101 and generate a short maintenance summary report." --classification CONFIDENTIAL` |
| 3 | Task receives `task_id` | Query Router | Query Router creates the `Task` row (owns creation) and returns `task_id` to the CLI immediately. |
| 4 | Query Router classifies task | Query Router | Rule-based: slash command `/task` ⇒ class `TASK`; `--classification` flag is taken as authoritative (user-declared, not inferred). |
| 5 | Orchestrator receives task | Query Router → Orchestrator | Synchronous `POST /internal/orchestrate` with the canonical handoff payload (§4.5). Orchestrator now owns task execution and status. |
| 6 | Orchestrator generates a plan | Orchestrator → Model Router (reasoning model) | One structured-output call producing a 3-step Plan (§5.2): `rag.search` → `python.execute` → `generate_report`. |
| 7 | Agent executes step 1 (permitted) | Agent loop | `THINK` → emits action `{"action":"rag.search","arguments":{"query":"Pump P-101 maintenance history"}}`. |
| 8 | Tool Gateway validates capability | Tool Gateway | Verifies the capability JWT issued for this step (signature, expiry, operation/scope match) — local check, no Control Plane round-trip. |
| 9 | Policy returns ALLOW | Tool Gateway → Control Plane (Policy Engine) | Resource is now known (retrieval target = maintenance docs, department=maintenance); rule "RAG search within authorized classification" → `ALLOW`. |
| 10 | Tool accesses authorized data | Tool Gateway → Data Plane | ACL/classification filtering happens inside the Data Plane, before results leave it. |
| 11 | Evidence recorded with provenance + ACL | Data Plane → Agent (via Tool Gateway) | Each evidence item carries `classification`, `acl`, `provenance_id` inline (§4.5 Evidence schema). |
| 12 | Agent executes step 2 (sandboxed compute) | Agent → Tool Gateway → Execution Service | `python.execute` parses maintenance dates out of the retrieved text and computes days-since-last-service, inside a one-shot container with no Docker socket, no host mount, no network. |
| 13 | Agent produces a report | Agent → Tool Gateway → Artifact service | `generate_report` renders the one fixed Markdown/DOCX template with the evidence + computed fields. |
| 14 | Artifact is verified | Verifier (in-process module) | Five structural checks (§6.7); all pass ⇒ `CANDIDATE → VERIFIED`. |
| 15 | Artifact enters REVIEW_REQUIRED | Artifact + Approval | `Approval` row created, `Approval.state = REVIEW_REQUIRED`. |
| 16 | Human approves | Approver (CLI `/approve`) → Control Plane → single transactional endpoint | `approver_id` comes from the session, never from the request body. |
| 17 | Artifact RELEASED, task COMPLETED | Approval endpoint | One transaction: `Approval=APPROVED`, `Artifact=RELEASED`, `Task=COMPLETED`, events emitted. |

The CLI's `/trace <task_id>` prints the full ordered event log at any point, proving the chain end to end.

### 1.2 Denial path (mandatory)

The demo deliberately makes the agent attempt one out-of-scope retrieval: a query whose target documents are tagged `acl: ["finance"]`, outside the task's granted `department: "maintenance"` scope.

```
Agent → rag.search(query="Q3 finance report") 
      → Tool Gateway: capability scope check passes (operation=rag.search is still valid)
      → Policy Engine: resource.acl ∩ agent.scope.acl = ∅  →  DENY
      → Tool Gateway returns {success:false, error:{code:"POLICY_DENIED"}}
      → tool is NOT executed, no data leaves the Data Plane
      → TOOL_DENIED event recorded with task_id, requested resource, and reason
```

This is surfaced identically to the happy path in `/trace` — the point of the demo is that a denial is a normal, observable, first-class outcome, not a crash.

### 1.3 Emergency-control path

```
Admin: citadel admin disable-tool python.execute
  → Policy Engine's static rule table gains one runtime override: tool_disabled["python.execute"] = true
Agent (later, same or new task) attempts python.execute with an otherwise-valid capability
  → Tool Gateway validates capability (still valid) → Policy Engine checks tool_disabled → DENY
  → TOOL_DENIED event recorded, reason="tool disabled by administrator"
```

This proves central, immediate revocation independent of any already-issued capability token — capabilities authorize *what an agent may attempt*; policy (checked on every call) decides *whether it is currently allowed*, and only policy needs to change for this to work.

---

## 2. MVP Trust Model & Process Topology — Resolving BB-042

This is the foundational decision every other section depends on, so it comes first.

**Decision:** Two trust zones, not one, and not five.

```
┌───────────────────── TRUSTED WORKFLOW ZONE (one process) ─────────────────────┐
│                                                                                 │
│  CLI-facing API · Query Router · Orchestrator · Control Plane                 │
│  (Identity, Policy, Capability, Approval) · Model Router · Data Plane         │
│  (Postgres + vector store access, RAG)                                        │
│                                                                                 │
│  These components never run adversarial/untrusted code and never execute      │
│  content an agent supplies. Consolidating them into one FastAPI process is    │
│  an explicit MVP simplification for build speed — it does NOT weaken the      │
│  guarantee that actually matters (see below).                                 │
└───────────────────────────────────┬────────────────────────────────────────────┘
                                     │  HTTP, over a Docker network the sandbox
                                     │  zone cannot see any other route on
                                     ▼
┌────────────────────── ISOLATED EXECUTION ZONE (separate) ─────────────────────┐
│                                                                                 │
│  Execution Service (holds the only Docker socket in the system)               │
│         │                                                                     │
│         ▼                                                                     │
│  One-shot `python.execute` container: no Docker socket, no host mount,       │
│  no network route to Postgres/vector store/internet, resource + time limits, │
│  destroyed immediately after the call returns.                                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Why this specific line, and not another one:** The audit's concern (BB-042) was that *nothing* in the architecture confirmed agents couldn't reach privileged resources directly. The one place that concern is concrete and dangerous is exactly where model-generated code runs — `python.execute` is the only capability in this slice that executes content the system did not author. Everything in the "trusted workflow zone" is Citadel's own code calling Citadel's own code; there is no adversarial input crossing a boundary inside that zone. The boundary that has to be real is the one around code execution, and it is:

- The Execution Service is a separate OS process (its own container in Docker Compose for the demo).
- It is the **only** thing in the whole system holding a Docker socket.
- The one-shot containers it creates have no Docker socket, no host filesystem mount beyond a scratch input/output directory, and no network — closing BB-016's contradiction directly (see §6.6).
- Agent containers/processes never exist as a separate runtime at all in this MVP — "the agent" is a loop inside the Orchestrator process (§3) that never runs untrusted code itself; it only *requests* that the Execution Service run code on its behalf, through the Tool Gateway.

**What this MVP explicitly does not claim:** Control Plane is not isolated from the Orchestrator by a process boundary in this slice — a compromised Orchestrator process could, in principle, bypass the in-process Policy Engine call. This is stated outright rather than hidden. For a production system this boundary would need to become a second real process; for a 36-hour vertical slice, the code-execution boundary is the one whose absence would be indefensible in a demo, so it is the one that is real.

---

## 3. Canonical Domain Model

One schema per object, owned by exactly one service in the trusted workflow zone. No subsystem invents its own copy.

```jsonc
// User — owned by Control Plane (Identity)
{
  "user_id": "U123",
  "username": "j.rao",
  "roles": ["engineer"],        // engineer | approver | admin
  "clearance": "CONFIDENTIAL",
  "department": "maintenance"
}

// Task — owned by Query Router (created) / Orchestrator (executed)
{
  "task_id": "T123",
  "user_id": "U123",
  "classification": "CONFIDENTIAL",
  "status": "RUNNING",           // see §4 Task state machine
  "requirements": {"needs_rag": true, "needs_document_generation": true},
  "created_at": "2026-09-06T10:00:00Z"
}

// Agent — a logical actor identity, owned by Orchestrator.
// NOTE (resolves BB-014): exactly one Agent record exists per task in this MVP.
// "agent_type" on a Plan step is a label used to select a prompt/tool-permission
// profile for that step — it does NOT spawn a new sandboxed process or a new
// Agent identity. There is no Supervisor pattern and no multi-agent delegation.
{
  "agent_id": "A123",
  "task_id": "T123",
  "agent_type": "researcher",     // researcher | writer — informational only
  "status": "RUNNING"             // RUNNING | SUCCESS | FAILED | MAX_STEPS
}

// Capability — owned by Control Plane, issued per plan step, verified by Tool Gateway
{
  "capability_id": "CAP123",
  "task_id": "T123",
  "agent_id": "A123",
  "operation": "rag.search",
  "scope": {"classification_max": "CONFIDENTIAL", "department": "maintenance"},
  "expires_at": "2026-09-06T10:05:00Z",
  "signature": "..."               // HMAC over the above fields, see §6.5
}

// Evidence — owned by Data Plane, returned through Tool Gateway
{
  "evidence_id": "E001",
  "document_id": "DOC-P101-HIST",
  "document_version": "1",
  "page": 4,
  "text": "...",
  "classification": "CONFIDENTIAL",
  "acl": ["maintenance", "engineering"],
  "provenance_id": "E001"          // MVP: provenance IS the evidence row itself, see §6.9
}

// Artifact — owned by Artifact service
{
  "artifact_id": "ART123",
  "task_id": "T123",
  "version": 1,
  "type": "maintenance_summary_report",
  "status": "TEMP",                 // see §4 Artifact state machine
  "hash": null,                     // populated once VERIFIED
  "provenance": ["E001", "E002"]    // list of evidence_ids actually cited
}

// Approval — owned by Control Plane, state changes owned by the single
// approval-decision endpoint (§6.10, resolves BB-047)
{
  "approval_id": "APR123",
  "artifact_id": "ART123",
  "approver_id": "U456",            // derived from session, never from request body
  "decision": null,                 // APPROVED | REJECTED
  "comment": null,
  "timestamp": null
}

// Event — owned exclusively by the single Observability writer (§6.11)
{
  "event_id": "EVT0001",
  "task_id": "T123",
  "actor_id": "A123",
  "event_type": "TOOL_DENIED",
  "payload": {"...": "..."},
  "timestamp": "2026-09-06T10:02:31Z",
  "previous_hash": "3f2a...",
  "hash": "9c1b..."
}
```

---

## 4. The Three State Machines — Resolving C-001

Task, Artifact, and Approval stay as three separate, minimal state machines. They are never merged, and the mapping between them is explicit rather than assumed.

**Task**
```
CREATED → PLANNING → RUNNING → WAITING_FOR_APPROVAL → COMPLETED
                         ↓
                       FAILED
```
*(`REVISION_REQUIRED` exists as a transient sub-state of RUNNING for the one scoped case in §5.3; it is not a terminal state.)*

**Artifact**
```
TEMP → CANDIDATE → VERIFIED → APPROVED → RELEASED
```

**Approval**
```
NOT_REQUIRED → REVIEW_REQUIRED → APPROVED / REJECTED
```

**Mapping table** — the exact correspondence an implementer needs, and the only place these three intersect:

| Task | Artifact | Approval | Trigger |
|---|---|---|---|
| RUNNING | TEMP | NOT_REQUIRED | Report generated by agent, not yet checked |
| RUNNING | CANDIDATE | NOT_REQUIRED | Verifier begins checking |
| WAITING_FOR_APPROVAL | VERIFIED | REVIEW_REQUIRED | All 5 verification checks pass (§6.7) |
| WAITING_FOR_APPROVAL | VERIFIED | APPROVED | Approver decision recorded, transaction in flight |
| **COMPLETED** | **RELEASED** | **APPROVED** | Single transaction commits (§6.10) — terminal, happy path |
| RUNNING (→ FAILED) | TEMP (rejected) | NOT_REQUIRED | Verifier check fails → MVP treats this as task FAILED, not auto-revision (§7, BB-038) |
| RUNNING → REVISION_REQUIRED → RUNNING | — | REJECTED | Approver rejects → Orchestrator re-enters RUNNING for one bounded revision (§5.3) |

---

## 5. Agent Contract & Orchestrator — Resolving BB-001, BB-002, BB-003

### 5.1 Agent execution loop (BB-001)

The agent is a deterministic loop, not a framework. It is a plain function inside the Orchestrator process, executed once per plan step:

```
THINK        → given the step's action + current evidence, decide the concrete arguments
ACTION       → emit exactly one structured action: {"action": str, "arguments": {...}}
OBSERVATION  → receive the Tool Gateway's result envelope (success/result or error)
DECISION     → CONTINUE (next step) | RETRY (same step, ≤1 retry) | TERMINATE
```

Termination states: `SUCCESS`, `FAILED`, `MAX_STEPS` (configurable, default `6`), `WAITING_FOR_APPROVAL` (reached after the last plan step succeeds and an artifact is produced). The agent never calls a tool directly — every `ACTION` is routed through the Tool Gateway (§6.6), and the agent never sees a capability token it wasn't issued for that specific step.

### 5.2 Plan generation (BB-002)

One structured-output call to the Model Router's reasoning model, using a fixed prompt template and a JSON Schema the response is validated against:

```json
{
  "plan_id": "P123",
  "task_id": "T123",
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

If the model's output fails schema validation, one repair prompt is sent ("your last output was invalid JSON against this schema: ..."); a second failure marks the task `FAILED`. No planning algorithm beyond this exists for the MVP — the plan shape for this scenario is effectively fixed, and the LLM call exists to prove the mechanism, not to demonstrate open-ended planning.

### 5.3 Plan revision (BB-003)

Scoped to exactly one case: an approver **REJECT**s a VERIFIED artifact. On rejection, the Orchestrator:

1. Keeps all completed steps' evidence and events untouched.
2. Discards only the `generate_report` step's output artifact.
3. Re-runs `generate_report` once with the approver's `comment` appended to the prompt.
4. If the second attempt is also rejected, the task ends `FAILED` — no further revision loop.

There is no general replanning, no step re-ordering, and no mid-execution plan change for any other trigger in this slice.

---

## 6. Component Contracts

### 6.1 CLI → Query Router (BB-024)

`Authorization: Bearer <session JWT>`, obtained once via `POST /login {username, password}` at CLI startup. No other credential form exists for the MVP.

### 6.2 Query Router → Orchestrator (BB-004, C-005)

**Canonical handoff — the single contract that was entirely missing from the architecture:**

```json
// POST /internal/orchestrate  (synchronous)
{
  "task_id": "T123",
  "user_id": "U123",
  "classification": "CONFIDENTIAL",
  "task_type": "DOCUMENT_ANALYSIS",
  "requirements": {"needs_rag": true, "needs_document_generation": true}
}
```

Ownership, resolved explicitly:
- **`task_id` is created by the Query Router**, before this call — never by the Orchestrator.
- **The Orchestrator owns all task state from this point on.** `GET /tasks/{id}` and `GET /tasks/{id}/trace` are served *only* by the Orchestrator — this also resolves C-005's three-way ownership ambiguity; Control Plane and Data Plane never expose a competing status endpoint.
- Synchronous for the MVP (the demo scenario runs in seconds; no async job queue needed).
- Error response: `{"error": {"code": "TASK_ALREADY_RUNNING" | "INVALID_REQUIREMENTS", "message": "..."}}`.

### 6.3 Orchestrator → Model Router (C-003, BB-009)

**One canonical schema, used for both the planning call and any other model call in this slice:**

```json
// Request
{"task_id": "T123", "required_capabilities": ["reasoning"], "classification": "CONFIDENTIAL"}

// Response — the ONE shape; the older single-model shape from 02_QUERY_MODEL_ROUTER.md
// is retired for this slice, not run in parallel with this one.
{
  "selected_models": {"reasoning": "qwen3-local", "embedding": "bge-base-local"},
  "routing_reason": "local_confidential_reasoning",
  "fallback_chain": []
}
```

Selection logic (BB-006, deliberately trivial): a static lookup table, not a scoring formula —

```
classification requires local-only  →  reasoning: qwen3-local
embedding always needed             →  embedding: bge-base-local
```

Model availability (BB-008): no health-check, no fallback chain population (`fallback_chain` stays `[]`). If the single local model call fails outright, the task transitions to `FAILED`. This is a stated MVP limitation, not an oversight — see §7.

Model manifest gains exactly one new field to close BB-007: `"max_classification": "CONFIDENTIAL"`. A task whose classification exceeds a model's `max_classification` fails routing outright (`FAILED`, reason `MODEL_CLASSIFICATION_INCOMPATIBLE`).

### 6.4 Control Plane — Identity (BB-023, BB-024)

A single local table of users (bcrypt-hashed passwords) is the identity provider for the MVP — explicitly documented as a development stand-in, not the LDAP/AD integration the full architecture names. `POST /login` issues a signed session JWT (8h expiry) carrying `{user_id, roles}`. **Every subsequent endpoint derives the acting identity from this verified JWT — a client-supplied `user_id` in a request body is never trusted and is ignored if present.** This applies with no exception to the approval endpoint (§6.10): `approver_id` always comes from the session, never from the request payload.

### 6.5 Control Plane — Capability issuance (BB-020)

Format: a signed token (HMAC-SHA256, single trusted issuer — the Control Plane process itself, since it and the verifier share a process in this MVP's trust zone per §2), short TTL (5 minutes), scoped to exactly one operation and one task/agent pair:

```json
{
  "capability_id": "CAP123", "task_id": "T123", "agent_id": "A123",
  "operation": "rag.search",
  "scope": {"classification_max": "CONFIDENTIAL", "department": "maintenance"},
  "expires_at": "2026-09-06T10:05:00Z"
}
```

Issued once per plan step, immediately before that step executes (not all at once at task start — this keeps the blast radius of a leaked token to one operation). **No revocation list exists for the MVP** — the 5-minute TTL is the only expiry mechanism. This is deliberately survivable: revoking a *tool* (§6.8) is enforced at the policy layer on every call, independent of any capability already issued, so `DISABLE TOOL` still works correctly even against a still-valid capability token.

### 6.6 Tool Gateway — the authorization spine (BB-015, C-002, BB-017)

**One topology, resolving the contradiction directly** — capability and policy are two different, complementary checks, not two competing versions of the same check:

```
Agent → Tool Gateway
           │
           ├─ Step A: verify capability (signature, expiry, operation match) — LOCAL, no network call
           │
           ├─ Step B: policy decision — Tool Gateway calls Control Plane's Policy Engine ONCE per
           │          call, with the concrete resource now known (capability only proves "this agent
           │          may attempt rag.search in general"; policy decides "is this specific document
           │          allowed right now")
           │
           └─ Step C: route to the right backend by `tool` name (Data Plane, or Execution Service),
                      and return one uniform envelope regardless of which backend answered
```

Uniform result envelope (closes BB-017 — this is the one shape every tool, sandboxed or not, returns through):

```json
// success
{"success": true, "tool": "rag.search", "result": {"...": "..."}, "error": null,
 "metadata": {"execution_id": "..."}}

// failure (capability OR policy OR execution failure — same shape, different `error.code`)
{"success": false, "tool": "rag.search", "result": null,
 "error": {"code": "POLICY_DENIED", "message": "..."}}
```

`error.code` values used in this slice: `CAPABILITY_INVALID`, `CAPABILITY_EXPIRED`, `POLICY_DENIED`, `TOOL_DISABLED`, `EXECUTION_ERROR`.

### 6.7 Policy Engine (BB-019)

Inputs and outputs exactly as specified; implementation is an ordered check list, not a rules engine:

```python
def decide(user, agent, task, action, resource) -> "ALLOW" | "DENY" | "REQUIRE_APPROVAL":
    if tool_disabled.get(action.tool):                                   return "DENY"
    if action.tool == "host.shell":                                      return "DENY"
    if action.tool == "artifact.release":                                return "REQUIRE_APPROVAL"
    if resource.classification > task.classification:                   return "DENY"
    if set(resource.acl).isdisjoint(set([task.department])):             return "DENY"
    if action.tool in {"rag.search", "python.execute", "generate_report"}: return "ALLOW"
    return "DENY"   # fail closed — anything not explicitly matched is denied
```

Four rules, checked in this order, first match wins, default DENY. No policy language, no RBAC×ABAC combination logic beyond this — that generality is explicitly deferred (§7, BB-019 marked REQUIRED but resolved minimally, not fully).

### 6.8 Emergency control (BB-021)

Exactly one control: `DISABLE TOOL`. A single in-memory (or one-row-per-tool DB table) flag, `tool_disabled: dict[str, bool]`, checked first in the Policy Engine above. Toggled via `POST /admin/tools/{tool_name}/disable` (admin-role-only, per §6.4's identity model). `KILL TASK`, `KILL AGENT`, `DISABLE MODEL`, `GLOBAL NETWORK BLOCK`, `QUARANTINE ARTIFACT` are explicitly not built for this slice.

### 6.9 Data Plane / RAG (BB-036, BB-037, BB-027, BB-028, BB-040)

**Ingestion (BB-036):** every source document must ship with a sidecar metadata file; ingestion **rejects** any document without one — no default, no inference:

```
data/maintenance/pump_p101_history.pdf
data/maintenance/pump_p101_history.pdf.meta.json
  → {"classification": "CONFIDENTIAL", "acl": ["maintenance", "engineering"], "department": "maintenance"}
```

**Organizational Knowledge vs. RAG store (BB-026, BB-027):** declared identical for this MVP — there is exactly one vector store, and "Organizational Knowledge" is not a separate system. Working Memory is not persisted at all; it lives only in the Orchestrator's in-process call stack for the duration of one synchronous task execution.

**Tool Gateway → Data Plane contract (BB-040 — previously the one integration point left as prose):**

```json
// Request
{"operation": "rag.search", "query": "Pump P-101 maintenance history",
 "requester": {"task_id": "T123", "agent_id": "A123", "classification_max": "CONFIDENTIAL", "department": "maintenance"}}

// Response — ACL/classification filtering already applied INSIDE the Data Plane,
// never post-filtered by the Tool Gateway or the agent
{"results": [
  {"evidence_id": "E001", "document_id": "DOC-P101-HIST", "document_version": "1",
   "page": 4, "text": "...", "classification": "CONFIDENTIAL",
   "acl": ["maintenance", "engineering"], "provenance_id": "E001"}
]}
```

**Provenance (BB-028):** not a separate graph store for this slice — `provenance_id` on an evidence row *is* the evidence row's own primary key; an artifact's `provenance` field is simply the list of `evidence_id`s it actually cited. Multi-hop provenance traversal (evidence → model → prior artifact) is deferred.

### 6.10 Artifact + Verifier + Approval (BB-038, BB-039, BB-043, BB-045, BB-047)

**Template selection (BB-045):** exactly one template exists (`maintenance_summary_v1`); "selection" is a no-op.

**Verifier (BB-043):** a plain function called synchronously right after generation — not a network service, not an LLM judge:

```python
def verify(artifact) -> bool:
    checks = [
        artifact.file_exists_and_readable(),
        artifact.compute_and_store_sha256(),
        artifact.has_required_sections(["Summary", "Maintenance History", "Sources"]),
        all(e.classification <= task.classification for e in artifact.cited_evidence()),
        len(artifact.provenance) > 0,
    ]
    return all(checks)   # any failure ⇒ task FAILED for this slice (no auto-revision)
```

**Immutability (BB-039):** enforced at the API layer, not the storage layer — every mutating endpoint checks `if artifact.status == "RELEASED": reject()`. This is stated explicitly as a known MVP limitation (a true write-once store is out of scope).

**Approval propagation (BB-047 — the audit's other major missing hand-off):** one transactional endpoint closes the gap completely.

```json
// POST /approvals/{approval_id}/decision   (approver_id derived from session, per §6.4)
{"decision": "APPROVED", "comment": "Looks correct."}
```

On `APPROVED`, in one transaction: `Approval.decision = APPROVED`, `Artifact.status = RELEASED`, `Task.status = COMPLETED`; emits `APPROVAL_GRANTED` then `ARTIFACT_RELEASED`. On `REJECTED`: `Approval.decision = REJECTED`, `Task.status = REVISION_REQUIRED` momentarily, then the Orchestrator's scoped revision (§5.3) runs.

### 6.11 Shared state (BB-011, BB-044)

**Only the Orchestrator commits authoritative state** — the agent loop only *proposes* observations; it never has write credentials to Postgres. This is enforced two ways at once, not by convention alone:

1. **Network-level:** the code-execution containers (the only place anything resembling "the agent's own process" runs untrusted logic) have no route to Postgres at all — this is a Docker network policy, not an application-level promise.
2. **Application-level:** the Orchestrator process is the only holder of the Data Plane's write credential; the Tool Gateway's Data Plane contract (§6.9) only exposes read (`rag.search`) and a narrow write (`generate_report`'s artifact creation) — no generic state-mutation endpoint exists for anything to call.

Optimistic versioning is used for the one place true concurrent writes could occur (two near-simultaneous task-status updates): `expected_version` must match, else `409 CONFLICT` — and since this MVP runs one task at a time with a single sequential agent loop, **the general merge-algorithm problem (BB-011's original open question) does not arise in this slice**: a conflict here means retry the read-modify-write once, never a semantic merge of concurrent agent outputs.

### 6.12 Observability (BB-035)

**Single-writer serialization**, resolving the concurrency question the audit raised about the hash-chain formula: every service calls one internal function (not even a separate process for the MVP — a single Python module with a lock around the write path) to append an event; that module alone computes `hash = SHA256(payload + previous_hash)` and is the only writer to the event table. This sidesteps the general problem entirely rather than solving it — there is exactly one place total ordering could break, and it is single-threaded by construction.

Event types emitted in this slice: `TASK_CREATED, PLAN_CREATED, AGENT_STARTED, ACTION_REQUESTED, CAPABILITY_CHECKED, POLICY_DECISION, TOOL_EXECUTED, TOOL_DENIED, EVIDENCE_RETRIEVED, STATE_COMMITTED, ARTIFACT_CREATED, ARTIFACT_VERIFIED, APPROVAL_REQUESTED, APPROVAL_GRANTED, APPROVAL_REJECTED, ARTIFACT_RELEASED`.

### 6.13 Network (BB-031, BB-032, BB-046 — deferred to a coarse guarantee)

No per-connection attribution table, no live dashboard. The claim demonstrated is coarser and verified by a single automated test: **the execution-zone containers have no route to any external host** (Docker network with no default gateway to the internet), checked by attempting `curl https://example.com` inside a `python.execute` container as part of the test suite (§8) and asserting failure. This is a real, testable guarantee — just a much smaller one than the full architecture's per-connection audit trail.

---

## 7. Black-Box Closure Matrix

Every finding from the audit, classified and closed (or explicitly deferred) for this slice. "MVP Decision" is one line; full reasoning for the non-trivial ones is in §§2–6 above.

| ID | Status | MVP Decision |
|----|--------|---------------|
| BB-001 | REQUIRED | THINK→ACTION→OBSERVATION→DECISION loop, structured JSON action, 4 termination states (§5.1) |
| BB-002 | REQUIRED | One structured-output planning call, fixed 3-step schema, one repair retry then FAIL (§5.2) |
| BB-003 | REQUIRED | Revision scoped to one case only: reject → regenerate report once, preserving evidence (§5.3) |
| BB-004 | REQUIRED | Canonical handoff JSON; Query Router creates `task_id`, Orchestrator owns all state after (§6.2) |
| BB-005 | REQUIRED | Rule-based classification from slash command + user-declared `--classification` flag, no ML classifier |
| BB-006 | REQUIRED | Static lookup table replaces the scoring formula entirely for MVP (§6.3) |
| BB-007 | REQUIRED | Manifest gains one field, `max_classification`; hard-fail routing if task exceeds it (§6.3) |
| BB-008 | DEFERRED | No health-check, no fallback chain; single model failure ⇒ task FAILED |
| BB-009 | REQUIRED | One canonical response shape adopted, resolves C-003 simultaneously (§6.3) |
| BB-010 | DEFERRED | No separate retry/escalation layer; a failed model call is a failed task |
| BB-011 | REQUIRED | Sidestepped: single sequential writer (Orchestrator) means no concurrent-agent merge case exists (§6.11) |
| BB-012 | DEFERRED | No checkpoint/resume; a crashed process leaves the task in its last recorded status |
| BB-013 | REQUIRED | Execution Service is the sole container-lifecycle owner, invoked only via Tool Gateway (§2, §6.6) |
| BB-014 | NOT_APPLICABLE | One Agent identity per task; `agent_type` is a label, not a spawned sub-agent — no Supervisor pattern exists (§3) |
| BB-015 | REQUIRED | Capability (coarse, local check) + Policy (fine-grained, per-call) as two distinct, non-contradictory checks (§6.6) |
| BB-016 | REQUIRED | Execution Service is the only Docker-socket holder; agent loop itself never runs untrusted code (§2, §6.6) |
| BB-017 | REQUIRED | One uniform result envelope for every tool regardless of backend (§6.6) |
| BB-018 | DEFERRED | No crash recovery; a failed step fails the task, no retry/reassign/idempotency handling |
| BB-019 | REQUIRED | Four static, ordered rules, fail-closed default; no RBAC×ABAC combination logic (§6.7) |
| BB-020 | REQUIRED | Signed, 5-minute, single-operation capability token; no revocation list (relies on short TTL + per-call policy check) (§6.5) |
| BB-021 | REQUIRED | Exactly one control, `DISABLE TOOL`, enforced in the Policy Engine (§6.8) |
| BB-022 | NOT_APPLICABLE | Agents never hold credentials in this design — only the Tool Gateway/Data Plane hold backend credentials |
| BB-023 | REQUIRED | Local dev identity provider; identity always derived from verified session, never from request body (§6.4) |
| BB-024 | REQUIRED | One-time `/login`, cached Bearer JWT for the CLI session (§6.1) |
| BB-025 | REQUIRED | Orchestrator is sole owner of `/tasks/{id}` and `/tasks/{id}/trace`; resolves C-005 too (§6.2) |
| BB-026 | REQUIRED | Working Memory not persisted (in-process only); Organizational Knowledge declared identical to the RAG store (§6.9) |
| BB-027 | REQUIRED | Same decision as BB-026 — declared one system, not two, for this MVP |
| BB-028 | REQUIRED | Provenance is the evidence row itself; no separate provenance graph (§6.9) |
| BB-029 | NOT_APPLICABLE | No retention/purge logic; a single demo run keeps everything |
| BB-030 | NOT_APPLICABLE | No backup/restore procedure needed for a demo |
| BB-031 | DEFERRED | No per-connection attribution table; replaced by one coarse "no external route exists" test (§6.13) |
| BB-032 | NOT_APPLICABLE | Fully air-gapped demo; no controlled-connectivity (Mode B) request/grant interface needed |
| BB-033 | NOT_APPLICABLE | No trace summarization step; raw structured actions/observations are logged directly, no extra model call |
| BB-034 | NOT_APPLICABLE | No scored evaluation harness; verification is purely structural (§6.10), no LLM-judge metrics |
| BB-035 | REQUIRED | Single in-process serializing writer for all events; sidesteps concurrent hash-chain ordering entirely (§6.12) |
| BB-036 | REQUIRED | Mandatory sidecar metadata file per document; ingestion rejects anything missing one (§6.9) |
| BB-037 | REQUIRED | Evidence schema carries `classification`/`acl` end-to-end into the Verifier's check (§6.9, §6.10) |
| BB-038 | REQUIRED | Five structural checks, no LLM judge; any failure ⇒ task FAILED, no auto-revision loop (§6.10) |
| BB-039 | REQUIRED | Immutability enforced at the API layer (reject mutation of RELEASED artifacts); storage-layer write-protection deferred (§6.10) |
| BB-040 | REQUIRED | Explicit JSON request/response contract defined, filtering happens inside the Data Plane (§6.9) |
| BB-041 | NOT_APPLICABLE | Single always-on local model, single concurrent task; no GPU scheduling needed |
| BB-042 | REQUIRED | Two trust zones: one consolidated workflow process, one genuinely isolated execution zone (§2) |
| BB-043 | REQUIRED | Verifier is an in-process function, not a service (§6.10) |
| BB-044 | REQUIRED | Enforced by network policy (no route to Postgres from the execution zone) *and* credential scoping, not convention alone (§6.11) |
| BB-045 | REQUIRED | Exactly one template exists; selection is a no-op (§6.10) |
| BB-046 | NOT_APPLICABLE | No live dashboard; egress denial proven by one automated test instead (§6.13) |
| BB-047 | REQUIRED | One transactional decision endpoint updates Approval, Artifact, and Task state together (§6.10) |

**Contradictions:**

| ID | Status | Resolution |
|----|--------|------------|
| C-001 | RESOLVED | Three separate state machines (Task/Artifact/Approval) with an explicit mapping table — never merged (§4) |
| C-002 | RESOLVED | Capability = coarse pre-authorization; Policy = fine-grained per-call decision. Not competing versions of one check (§6.6) |
| C-003 | RESOLVED | One canonical Model Router response schema; the older shape is retired for this slice (§6.3) |
| C-004 | RESOLVED PROCEDURALLY | Build order (§8) implements and proves the security path (Identity/Policy/Capability/Tool Gateway, Step 4) *before* the Orchestrator is ever wired to a real tool call (Step 7) |
| C-005 | RESOLVED | Orchestrator is the sole owner of task status; Control Plane and Data Plane expose no competing endpoint (§6.2) |

**Totals:** 33 REQUIRED, 5 DEFERRED, 9 NOT_APPLICABLE.

---

## 8. Implementation Roadmap

Build in this order — each step should run and be demoable before the next begins. Suggested stack (substitutable, but concrete so nothing is left to decide mid-build): **FastAPI** for the trusted workflow zone, **Postgres** (or SQLite if faster to stand up) for all tables in §3, **a local vector store** (e.g. Chroma or Qdrant) for RAG, **Docker SDK for Python** inside the Execution Service, **a locally-served open-weight model** (e.g. via Ollama) for reasoning + embeddings.

| Step | Deliverable | Proves |
|---|---|---|
| 1 | Inspect any existing code/repo structure before writing anything new | No blind overwrites |
| 2 | Freeze this document as the implementation contract (domain model, schemas, state machines above) | One source of truth |
| 3 | Foundation tables: `User, Task, Agent, Event, Artifact, Approval` (§3) + the single Observability writer (§6.12) | Every later step has somewhere to record itself |
| 4 | Security path: Identity (§6.4) → Policy (§6.7) → Capability (§6.5) → Tool Gateway (§6.6), tested with a fake tool that just echoes | ALLOW and DENY both work *before* any real tool exists — resolves C-004 |
| 5 | Execution Service + one-shot `python.execute` container, no Docker socket inside it (§2, §6.6) | The one real process boundary is real, independently of the rest of the system |
| 6 | RAG: ingestion with mandatory ACL sidecar (§6.9) → embed → retrieval filtered by ACL/classification *before* results return | BB-036/037/040 all provable together |
| 7 | Orchestrator: task → plan (§5.2) → agent loop (§5.1) → real tool calls through the now-working Tool Gateway → finish/revise (§5.3) | The whole PLAN→ACT→OBSERVE loop, for real, against real tools |
| 8 | Artifact pipeline: generate → verify (§6.10) → approve (§6.10) → release, with the state-machine mapping (§4) enforced | The full happy path, end to end |
| 9 | CLI: `login`, `/task`, `/status`, `/approve`, `/trace`, `admin disable-tool` — only after step 8 works | The demo is drivable by a human, not just by test code |

Each step should conclude with the relevant rows of §9 passing before moving to the next.

---

## 9. Testing Checklist

**Security**
- [ ] Authorized tool call → `ALLOW`
- [ ] Unauthorized classification → `DENY`
- [ ] Unauthorized ACL/department → `DENY` (the denial-path demo, §1.2)
- [ ] Expired capability → `DENY`
- [ ] Disabled tool → `DENY` even with a valid capability (the emergency-control demo, §1.3)
- [ ] No code path exists for the execution zone to reach Postgres directly
- [ ] No code path exists for the execution zone to reach the Docker socket

**Orchestration**
- [ ] Task → plan produces valid, schema-conformant JSON
- [ ] Each plan step's action reaches the Tool Gateway, never a tool directly
- [ ] Observation feeds correctly into the next step's THINK
- [ ] Agent terminates on `SUCCESS`, `FAILED`, and `MAX_STEPS` correctly

**RAG**
- [ ] Correctly-tagged, authorized document → retrieved
- [ ] Correctly-tagged, out-of-scope document → filtered before reaching the agent
- [ ] Document missing its ACL sidecar → ingestion rejected outright

**Artifact**
- [ ] Generate → verify → approve → release, full happy path
- [ ] A RELEASED artifact rejects any further mutation attempt
- [ ] A verification failure marks the task `FAILED` (no silent pass)

**Audit**
- [ ] Every event type in §6.12's list is actually emitted at least once during the happy-path run
- [ ] `/trace` shows the denial-path event (`TOOL_DENIED`) and the emergency-control event, not only the happy path
- [ ] The hash chain is unbroken end to end for one full task run

---

## 10. What This Slice Deliberately Does Not Build

Kubernetes, Kafka, Redis, a distributed scheduler, a policy DSL, a multi-agent framework, Docker-socket access for agents, direct agent writes to shared state, internet access for the execution zone, an LLM-as-judge verifier, a polished dashboard, or new frameworks without a concrete requirement above. Every one of these maps to a `DEFERRED` or `NOT_APPLICABLE` row in §7 — none is an accidental omission.

---

## 11. Definition of Done

The slice is done when one documented startup sequence can demonstrate, in order: an authenticated task submission reaching the Query Router; the Orchestrator producing a structured plan; an agent action passing capability and policy checks; a sandboxed `python.execute` call with no Docker socket inside it; ACL-filtered evidence retrieval; a generated, structurally-verified artifact; a human approval that atomically releases it; **and**, separately, an out-of-scope retrieval attempt that is denied without executing, **and** a `DISABLE TOOL` command that revokes a capability's practical effect immediately — with `/trace` showing the complete, hash-chained event history for all three of those runs.

