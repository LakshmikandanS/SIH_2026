# ADR-0010 — "Web search" is the offline reference library

**Status:** Accepted · **Date:** 2026-09-23
**Relates to:** invariant 10 (no egress), ADR-0001 §Q2 (air-gapped by construction),
ADR-0008 (the report-writing workbench).

---

## Context

The vision's report on lathe L-1 needs facts that the company's own records do not hold:
what a CNC turning centre costs, what a retrofit achieves, and how long operators take to
train. An engineer would search the web for them. Citadel cannot. There is no egress, and
the demonstration proves it live (target E).

Still, "search the web" is how people describe that step, and the vision names web search
among the tools. Pretending the step does not exist makes reports thinner. Letting a tool
open a socket, even "just for search", breaks the product's central claim.

## Decision

**`web.search` is a search over the offline reference library.** The library holds
external literature — vendor catalogues, technology digests and standards extracts —
that a person imported through review. The step keeps its name and does its job without
a network.

- **One store, one predicate.** Reference documents are ordinary documents in the folder
  `REFERENCE_LIBRARY`, ingested, versioned and ACL-filtered like everything else. There is
  no second index and no second access path.
- **The split is data.** In `registry/tools.yaml`, `web.search` carries
  `options: {within_folder: REFERENCE_LIBRARY}` and `docs.search` carries
  `outside_folder: REFERENCE_LIBRARY`. Retrieval turns both into a clause in the same SQL
  statement as the ACL predicate. Company records and outside literature stay apart in
  what an agent finds, so a report can say which is which. No code branches on a tool
  name.
- **Honest labelling.** The tool's description says what it is: "the air-gapped stand-in
  for a web search. Nothing leaves this machine." The activity feed and journal call it
  "web search" (the step a person recognises). Evidence from it cites the imported
  document, its version and page, as any evidence does.
- **Getting literature in** is an upload into the `REFERENCE_LIBRARY` folder, which is a
  person's reviewed act, audited like any upload. The demo corpus seeds three such
  documents (`ops/demo/corpus/manifest.yaml`).

## What this gives up

- **The library is only as current as its last import.** A price in a catalogue imported
  in September is September's price, and the citation shows the document and its date.
  That is more honest than a live page that may change after the report is written.
- **No discovery.** An agent cannot find literature nobody imported. It says what it could
  not find, and the report's open questions carry it to a person.

## Revisit when

- An organisation runs an internal mirror or proxy it trusts (a vetted standards portal on
  its own network). That becomes another folder or another tool behind the same
  chokepoint, never an exception to the egress rule.
