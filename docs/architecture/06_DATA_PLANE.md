# Block 06 — Data Plane / Shared State & Memory

## Role

The Data Plane contains the information agents and humans collaborate around.

It should be treated as multiple stores rather than one giant database.

## Recommended layout

```text
                DATA PLANE
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
    Postgres       Qdrant      Object Store
    state          vectors     artifacts
       │
       └──── Redis (optional cache/queue)
```

## 1. Task state store — Postgres

Store:

- task metadata
- plan versions
- agent states
- decisions
- assumptions
- approvals
- policy references
- event index
- artifact metadata

Example:

```sql
tasks(
  task_id,
  owner_id,
  objective,
  classification,
  status,
  current_phase,
  state_version,
  created_at,
  updated_at
)
```

## 2. Shared state

Use optimistic concurrency.

```text
Agent reads version 12
Agent prepares update
Agent submits "write if version == 12"

If current version == 12:
    commit as version 13
Else:
    conflict → reload → merge
```

This prevents concurrent agents from silently overwriting one another.

## 3. Vector knowledge store

Recommended:

- Qdrant
- Chroma for a simpler prototype

Each chunk should have metadata:

```json
{
  "document_id": "manual_17",
  "version": 4,
  "department": "maintenance",
  "sector": "1",
  "classification": "CONFIDENTIAL",
  "acl": ["maintenance_team"],
  "page": 27
}
```

## 4. ACL-aware retrieval

The query itself should carry authorization filters.

```text
user permissions
      ↓
policy engine
      ↓
retrieval filter
      ↓
vector search
      ↓
reranking
      ↓
evidence
```

Do not retrieve broadly and filter after exposing results to the model.

## 5. Artifact store

Use object storage such as MinIO for:

- DOCX
- PPTX
- XLSX
- PDFs
- scans
- generated datasets
- code bundles

Artifact metadata remains in Postgres.

## 6. Memory types

Separate:

```text
Working memory
→ current agent execution

Task memory
→ facts/decisions for one task

Organizational knowledge
→ persistent approved knowledge base

Execution history
→ immutable events
```

Do not mix them into one generic "memory" object.

## 7. Provenance

Every piece of generated evidence should point to:

```text
source document
source version
page/section
retrieval event
agent/model that used it
artifact where it appeared
```

## 8. Retention

Classification-sensitive data should have configurable retention:

```text
temporary task data
→ purge after policy-defined window

approved artifacts
→ organizational retention policy

audit events
→ immutable retention policy
```

## 9. Backup

For sovereign deployments, backups must also remain inside the approved infrastructure.

The backup plan must cover:

```text
Postgres
vector DB
object store
policy configuration
model manifests
audit events
```
