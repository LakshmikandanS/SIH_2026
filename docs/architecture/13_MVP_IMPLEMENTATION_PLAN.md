# Block 13 — MVP Implementation Plan

## Phase 1 — Foundation

Build:

```text
Local model serving
Docker sandbox
Postgres
basic object storage
CLI
```

Deliverable:

```text
/talk or /task → local model → response
```

## Phase 2 — Core orchestration

Build:

```text
Query Router
Model Router
Orchestrator
Task State
Checkpointing
```

Deliverable:

```text
/task
→ plan
→ tool
→ observe
→ revise
```

## Phase 3 — Control Plane

Build:

```text
Identity
Policy engine
Capability grants
Audit events
Approval gate
```

Deliverable:

```text
agent cannot use a tool without policy approval
```

## Phase 4 — RAG

Build:

```text
ingestion
OCR
embeddings
Qdrant
ACL filters
source provenance
```

Deliverable:

```text
internal SOP → grounded answer
```

## Phase 5 — Deliverables

Build:

```text
DOCX builder
verification
approval
artifact versioning
```

Deliverable:

```text
scan → report → approval → released DOCX
```

## Phase 6 — Zero-egress demonstration

Build:

```text
firewall deny-by-default
network telemetry
blocked-connection audit
```

Deliverable:

```text
agent attempts external connection
→ blocked
→ audited
→ dashboard shows proof
```

## Phase 7 — Multi-agent

Add:

```text
retrieval agent
vision agent
analysis agent
document agent
supervisor
```

Demonstrate shared-state coordination.

## Suggested repository

```text
sovereign-workbench/
├── cli/
├── router/
│   ├── query/
│   └── model/
├── orchestrator/
├── control/
│   ├── policy/
│   ├── identity/
│   ├── approvals/
│   └── audit/
├── execution/
│   ├── sandbox/
│   └── tools/
├── data/
│   ├── state/
│   ├── rag/
│   └── artifacts/
├── network/
├── observability/
├── models/
│   ├── manifests/
│   └── serving/
├── tests/
└── docs/
```

## Demo priority

The strongest demo is not "look at our architecture."

It is:

```text
CONFIDENTIAL SCAN
      ↓
OCR / VISION
      ↓
LOCAL RAG
      ↓
MULTI-AGENT ANALYSIS
      ↓
DRAFT APPROVAL NOTE
      ↓
AUTOMATED VERIFICATION
      ↓
HUMAN APPROVAL
      ↓
RELEASED DOCX
      ↓
FULL TRACE
      ↓
ZERO EGRESS PROOF
```

That single workflow demonstrates nearly every architectural claim.
