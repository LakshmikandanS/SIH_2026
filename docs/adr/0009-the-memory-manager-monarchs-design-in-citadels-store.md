# ADR-0009 — The memory manager: Monarch's design, in Citadel's store

**Status:** Accepted · **Date:** 2026-09-23
**Supersedes:** ADR-0001 §Q9 (how Monarch is consumed), whose *Revisit when* came due:
memory was needed before Monarch changed. Also `packages/memory/AGENTS.md`
§"Monarch is a dependency, never a merge" and §"The seam does not exist yet".
**Keeps:** Q9's hard constraint. The repositories are not merged, and no Monarch code is
copied. **Amends:** the module map in the root `AGENTS.md`: `tools` may now import
`memory`.

---

## Context

Fahim asked for "the monarch idea of grounding AI stateful by the memory manager" in
Citadel. Monarch (`AI_WORKBENCH/MONARCH`) keeps a local assistant grounded in what it
learned before. It runs one pipeline — extract candidate memories, retrieve the related
ones, let a model choose one of six operations, execute that choice deterministically —
and scores retrieval by meaning and recency.

The plan had been to install Monarch as a pinned package once it grew a seam. Reading its
code (recorded in `packages/memory/AGENTS.md` before this ADR), three things were missing:

- **no scope on a memory**: no owner, classification or ACL;
- **a global search, filtered afterwards in Python**: the exact pattern invariant 5
  forbids for documents;
- **module-level singletons**: nowhere to pass a caller's scope.

Organisational memory is no less sensitive than the corpus it was learned from. Wiring
Monarch in as it stands would put a post-filtered store behind the workbench. It would
also add SQLite and LanceDB beside Postgres, and fixing Monarch upstream first would
have left memory unbuilt for the demonstration.

## Decision

**Implement Monarch's design in Citadel's own store.** This is a re-implementation of
the idea, not a copy of its code and not a dependency on its package. The code is in
`packages/memory` (`manager.py`, `consolidate.py`) and migration `0010`.

- **Where memories live.** Two Postgres tables: `memories` and `memory_events`. Each
  memory has a tier (`episodic`: what happened; `semantic`: what is true), an open
  `memory_type` checked by shape only (`equipment_fact`, `outcome`, `lesson` …), a
  subject, a pgvector embedding and a full-text vector.
- **Scope.** Every memory carries a **classification and an ACL, exactly as a document
  does**. The two ways a memory reaches Python are recall and the related-memory search.
  In both, **the visibility predicate is in the same SQL statement as the vector search**
  (invariant 5). A memory the caller may not see is never read.
- **Compartments.** A candidate is compared only with memories of **its own compartment
  and tier**: the same classification, the same ACL. Merging a CONFIDENTIAL finding into
  an INTERNAL memory would be a leak with extra steps, and episodic history is never
  merged into facts.
- **Monarch's six operations**: `create`, `update`, `merge`, `contradict` (the old memory
  is kept as superseded history), `ignore` and `archive`.
  - A model **proposes** one of them against the related memories, under a JSON schema
    whose `target` may only name a memory it was shown.
  - A deterministic executor **disposes**.
  - Anything unusable — a malformed decision, a missing target, a failed call — **falls
    back to `create`, the lossless choice**.
  - Every decision, `ignore` included, is a row in `memory_events` and an audit event.
- **What is remembered, and when.** Memory is written at a task's end, never as a side
  effect of a model turn.
  - The **outcome** is written without a model. When the same task ends again after a
    revision, that outcome is **updated**, not duplicated; the earlier wording stays in
    the event log.
  - An **approver's decision** is written without a model.
  - **Durable facts** are extracted by a model from the findings and answer, validated,
    then run through the mutation flow.
  - **People** state, edit, archive and restore memories within what they may see, from
    the Memory tab or `/remember`, and the same flow applies to what they state.
- **How it grounds the work.** Before planning, the lead **recalls** the top memories for
  the goal. That call goes through the policy chokepoint as the `memory.recall` tool:
  it is signed, audited and filtered by the task's own scope. This is the new
  **`tools → memory` edge** in the module map. The plan prompt shows the memories as
  hints about where to look and what went wrong last time.
- **Memories are grounding, not evidence.** A deliverable cites documents (E#) and
  computations (C#), never a memory. The worst case of a stale memory is a wasted search,
  not a false sentence.

## What this gives up

- **Monarch and Citadel diverge.** An improvement to Monarch's pipeline does not reach
  Citadel by upgrading a package. The shared part is small: the six operations, the
  prompt's shape, and the scoring rule. It is named in `manager.py`'s header so that a
  port stays deliberate.
- **Recency is per day, not per hour** (`RECENCY_DECAY_PER_DAY = 0.98`). An organisation's
  memory ages more slowly than a person's chat. This is a judgement, not a measurement.
- **A model decides merges.** The mutation prompt runs on the same local models as
  everything else. A poor decision can merge two facts that should have stayed apart.
  Every merge is in the event log with the text it replaced, and a person can edit or
  restore.

## Revisit when

- Monarch grows the seam (scope on the record, a predicate pushed into the query,
  injected repositories). Depending on it could then replace this copy of its design, if
  the second storage engine is worth it on the demonstration box.
- Memory volume makes the per-candidate related search the slow part of a task's end.
