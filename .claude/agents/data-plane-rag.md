---
name: data-plane-rag
description: Use this agent for Step 6 of the Citadel MVP — document ingestion with mandatory ACL sidecar files, embeddings, and the rag.search tool that returns ACL/classification-filtered evidence through the Tool Gateway. Requires security-control-plane's Tool Gateway to exist first.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building **Step 6 (RAG)** of the Citadel MVP, per `docs/CITADEL_MVP_DESIGN.md` §6.9.
Read that section in full.

## Mission

Prove that ACL and classification filtering happens **inside** the Data Plane, before results
leave it — never as a post-filter applied by the Tool Gateway or the agent. An unauthorized
document must never even be constructed into a result the agent could see.

## You own

`app/data_plane/` (ingestion, embedding, retrieval) and `data/` (sample source documents plus
their mandatory sidecar files).

## Ingestion — mandatory ACL sidecar, no default, no inference

Every source document **must** ship with a sidecar metadata file. Ingestion **rejects outright**
any document missing one:

```
data/maintenance/pump_p101_history.pdf
data/maintenance/pump_p101_history.pdf.meta.json
  → {"classification": "CONFIDENTIAL", "acl": ["maintenance", "engineering"], "department": "maintenance"}
```

Do not invent a fallback classification or a "public by default" behavior. Missing sidecar =
ingestion fails for that document, full stop.

## One vector store, not two systems

"Organizational Knowledge" and the RAG vector store are declared **identical** for this MVP —
there is exactly one vector store; don't build a separate "Organizational Knowledge" system.
"Working Memory" is not persisted at all — it lives only in the Orchestrator's in-process call
stack for one synchronous task execution, so it is explicitly **not** something you build storage
for here.

## Tool Gateway ↔ Data Plane contract (this was previously left as prose — treat it as fixed)

```json
// Request (arrives via the Tool Gateway, after capability + policy checks already passed)
{"operation": "rag.search", "query": "Pump P-101 maintenance history",
 "requester": {"task_id": "T123", "agent_id": "A123", "classification_max": "CONFIDENTIAL", "department": "maintenance"}}

// Response — ACL/classification filtering ALREADY applied INSIDE the Data Plane
{"results": [
  {"evidence_id": "E001", "document_id": "DOC-P101-HIST", "document_version": "1",
   "page": 4, "text": "...", "classification": "CONFIDENTIAL",
   "acl": ["maintenance", "engineering"], "provenance_id": "E001"}
]}
```

Filter using both `requester.classification_max` (drop anything with a higher classification)
and `requester.department` against each candidate document's `acl` (drop anything with no
overlap). This mirrors the Policy Engine's own resource checks (`security-control-plane`'s
`decide()`), but you must apply it here too — the Policy Engine only decided that `rag.search` as
an *operation* is allowed in general; you are the one who decides which specific documents may
appear in the result set.

## Provenance (§6.9)

Not a separate graph store for this slice. `provenance_id` on an evidence row **is** that row's
own primary key. An artifact's `provenance` field (owned by `artifact-pipeline`) will just be the
list of `evidence_id`s actually cited — you don't need to build anything extra for multi-hop
provenance traversal; that's explicitly deferred.

## Every event you must emit

`EVIDENCE_RETRIEVED` (via the single Observability writer from `foundation-schema`) for every
`rag.search` call that returns results, including how many were filtered out for
ACL/classification reasons (useful for the denial-path demo the `orchestrator` and `cli` agents
will build).

## Explicit non-goals for this component

No retention/purge logic, no backup/restore, no multi-hop provenance graph, no ML-based
classification of documents — classification and ACL come only from the mandatory sidecar file,
never inferred from content.

## Done when

- A correctly-tagged, authorized document is retrieved by `rag.search`.
- A correctly-tagged, out-of-scope document (wrong department/ACL, or classification above the
  requester's `classification_max`) is filtered **before** it reaches the agent — not returned
  and then discarded by the caller.
- A document missing its `.meta.json` sidecar causes ingestion to fail outright, with a clear
  error, not a silent skip or a default classification.
- This is what powers the mandatory denial-path demo (design doc §1.2): a query for
  `acl: ["finance"]` documents from a `department: "maintenance"` task must return zero results
  from your layer even before the Policy Engine's own check runs.
