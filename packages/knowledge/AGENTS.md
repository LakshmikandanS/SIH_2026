# AGENTS.md — knowledge

Ingestion and retrieval. Documents in, citations out.

## Depends on

`contracts`, `platform`, `gateway` (for embedding and vision calls).
**`runtime` may not import this package** — retrieval is reached through the `docs.*`
tools so that it passes the policy chokepoint like everything else.

## Ingestion

`detect → extract | OCR | vision → normalise → chunk (layout-aware) → embed → index`

- Accept born-digital PDF, scanned PDF, images, Office documents, plain text.
- Emit a **normalised document**: pages → blocks (text / table / figure), each with a page
  index, a bounding box and a content hash.
- Documents are versioned. Citations pin to a version.

### Classification is mandatory at the door

A document without classification and ACL metadata is **rejected, loudly**. Never
defaulted. Defaulting a classification is how confidential material leaks, and a rejected
upload is a five-second fix while a mislabelled one is permanent.

### Vision runs here, at ingest — not in the agent loop

ADR-0001 Gap 2, kept by ADR-0002. OCR and vision extraction happen when the document is
uploaded, and the page/block output is persisted. By the time the agent loop runs, it reads
rows, not pixels.

On `demo-local` this keeps the model swap on a progress bar instead of mid-step. On `hpc-eval`
it is simply better: extraction becomes idempotent, cacheable, and re-runnable when a
better model lands. `vision.extract` still exists as a tool for genuinely re-reading a
region, and that call is budgeted as a swap.

## Retrieval

`ACL predicate → hybrid candidate generation (BM25 + dense) → fusion → rerank → citations`

### Filter before rank. This is the cardinal rule of this package.

A record the caller may not see is **never constructed into a result**. Not retrieved and
dropped — never retrieved. In Postgres that means the predicate is a `WHERE` clause in the
same query as the vector search, so the database enforces it and application code cannot
forget to.

Read the prototype's `app/rag/search.py` docstring before writing this. Its reasoning is
correct and worth matching; its linear scan is not.

**Keep the denial record.** The prototype emitted a `DeniedDocument` carrying document id,
classification, ACL and reason — deliberately never the text, so the denial record cannot
leak what it denies. Moving the filter into SQL loses this unless you deliberately keep it,
and without it "ACL filtering works" is unfalsifiable. It is also what the multi-user
demonstration (ADR-0001 §Q7) puts on screen.

## Citations

Document id, version, page, bounding box. Enough to highlight the exact region of a scan in
the UI. A citation that cannot be pointed at is not a citation.

## Reranking runs on CPU

App box, cross-encoder, top ~20. It contends with nothing and costs a few hundred
milliseconds. Do not put it on the GPU because there is room on `hpc-eval` — `demo-local`
is the design constraint, and it is the profile the demonstration runs on.
