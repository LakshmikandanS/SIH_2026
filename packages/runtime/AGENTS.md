# AGENTS.md — runtime

Planner, agent loop, replanner, step journal, budgets, cancellation, worker pool.

## Depends on

`contracts`, `platform`, `gateway`, `tools`, `memory`.
**Not `knowledge`. Not `deliverables`.** Both are reached through tools, so that every
access passes the policy chokepoint. This is enforced structurally — if you need retrieval
here, you need the `docs.search` tool, not an import.

## The loop

```
plan → select tool → invoke through the chokepoint → observe
     → decide: continue | retry | replan | finish
```

**Replanning is a first-class outcome, not an error path.** Observations must be able to
change the plan. A loop that can only execute its original plan is a script.

## The planner

Sees the **real free-text goal**. Produces a plan with no fixed shape and no fixed length.

The prototype held the demo task as a string constant, ignored the user's goal when
planning, told the model to emit exactly three steps, and then validated that it had. That
is not planning, and `validate_fixed_shape` and everything like it is discarded. If you
write a test that asserts a plan has N steps, you have rebuilt it.

## Observation handling

Dispatches on the tool's **declared schema**, never on its name. An `if/elif` over tool
names here is the specific thing that made the prototype's loop unextensible.

## The journal

Append-only, per task. **A task is reconstructible from its journal.** That property is
what makes resumption and cancellation real rather than aspirational — a restart mid-task
must not lose it.

No `UNIQUE(task_id)` on agents. Do not encode "one agent per task" as a schema fact.

## Budgets

Steps, tokens, wall clock. Checked **every iteration**, not at the end. Cancellation comes
from the UI and takes effect promptly — a cancel that waits for the current model call to
finish is acceptable; one that waits for the whole plan is not.

Queue wait counts against wall clock and is reported separately, so a task waiting on GPU
admission is visibly waiting rather than apparently hung.

## Concurrency

**Task concurrency is unbounded. GPU admission is bounded.** They are separate mechanisms
and conflating them is how the multi-user demonstration becomes a lie.

| Runs free (app box, CPU) | Queues on GPU admission |
|---|---|
| Ingestion, OCR, layout | Planning and replanning |
| Embedding, BM25, fusion, rerank | Reasoning steps |
| Sandbox execution | Vision re-reads |
| Document generation, verification | — |
| Approval, audit writes | — |

A semaphore in front of model calls, sized from the profile registry, is the mechanism.
Workers pull tasks freely and queue only at the model call.

### Two caps, not one (ADR-0004)

The demonstration runs on **one box**, so the CPU column above is no longer free. OCR,
embedding, reranking, Postgres, the sandbox and Ollama share a machine.

So there is a second, independent semaphore — **`cpu_admission`**, bounding concurrent
ingestion and OCR — also sized from the profile registry. The principle from §Q12 is
unchanged: bound concurrency at the scarce resource. On one box there are two of them.

Without this, a bulk corpus ingest running OCR across every core starves the API and the
UI looks hung, which will happen during a demonstration at the worst possible moment.

**The live task view must distinguish the two waits.** "Waiting on GPU" and "waiting on
CPU" are different problems with different fixes, and a task that shows neither reads as
hung.

## Execution never happens inside an HTTP request

Queue, worker pool, durable journal, SSE for progress, real cancellation. The prototype ran
whole agentic tasks inside one request and told users to expect 20–40 seconds.
