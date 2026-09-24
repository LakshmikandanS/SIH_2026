# AGENTS.md — memory

Three tiers. Working memory belongs to one task; episodic and semantic memory belong to the
workbench, and are kept by the **memory manager** — Monarch's design, in Citadel's store
([ADR-0009](../../docs/adr/0009-the-memory-manager-monarchs-design-in-citadels-store.md)).

## Depends on

`contracts`, `platform`, `gateway`. Reached by `tools` (the `memory.recall` tool) and by
`runtime` (writing what a finished task established).

## The tiers

| Tier | Scope | Where it lives |
|---|---|---|
| **Working** | One task: its plan, revision request, revision brief. Survives resumption | `WorkingMemory(db, task_id)`, Postgres |
| **Episodic** | What happened: a task's outcome, an approver's decision and comment | `memories` (tier `episodic`), Postgres |
| **Semantic** | What is true: equipment, costs, workload, vendors, conventions, lessons | `memories` (tier `semantic`), Postgres |

Memory writes are deliberate operations with a lifecycle — at a task's end, at a decision,
or by a person — never a side effect of a model turn.

## The pipeline (Monarch's)

```
text -> extract candidates -> for each: related memories (same compartment, same tier)
     -> a model proposes ONE of: create, update, merge, contradict, ignore, archive
     -> a deterministic executor disposes; anything unusable -> create (lossless)
```

- `MemoryManager.extract` asks a model for candidates and **validates** them (shape, type
  name, tier); it does not trust them.
- `MemoryManager.propose` runs one candidate through the flow. `use_model=False` stores it
  as proposed; `replaces_same_task=True` (a task's outcome) updates that task's earlier
  record of the same kind instead of adding a second one.
- Every decision — `ignore` included — is a row in `memory_events` and an audit event
  (`memory.create` … `memory.archive`, `memory.edit`, `memory.restore`).
- `consolidate.remember_task` / `remember_decision` are what `runtime` calls.

## Scope is the point

Every memory carries a **classification and an ACL, exactly as a document does**.

1. **Retrieval filters in SQL**, in the same statement as the vector search (invariant 5).
   `recall`, `browse`, `get` and the related-memory search never read a memory the caller
   may not see into Python. `test_memory_manager.py` checks the raw rows with a spy, and a
   negative control proves the same query does return them to someone cleared.
2. **A candidate is only compared with — and so can only mutate — memories of its own
   compartment** (same classification, same ACL) and tier. Merging a CONFIDENTIAL finding
   into an INTERNAL memory would be a leak with extra steps.
3. **People curate within what they may see**: state (`remember`), `edit`, archive and
   restore. Nothing else; `superseded` is the executor's word, not a person's.

## Memories are grounding, not evidence

An agent recalls memories before it plans (through the chokepoint, as `memory.recall`) to
know where to look and what went wrong last time. A deliverable cites documents (E#) and
computations (C#), **never a memory**. A stale memory costs a search, not a false sentence.

## Vocabulary

`memory_type` is open (`^[a-z][a-z0-9_]{1,40}$`). `SUGGESTED_TYPES` lists the common ones
(`equipment_fact`, `cost_fact`, `outcome`, `rejection`, `lesson` …); a model or a person
may use another. A closed enum here would be the closed-vocabulary failure the root
AGENTS.md names.

## Monarch

Monarch is not a dependency. Its store has no scope on a record, searches globally and
filters in Python afterwards, and keeps its repository in module-level singletons (ADR-0009
§Context). The shared part — the six operations, the mutation prompt's shape, meaning ×
recency scoring — is named in `manager.py`'s header so that a port from Monarch stays
deliberate. If Monarch grows the seam, depending on it becomes an option again.
