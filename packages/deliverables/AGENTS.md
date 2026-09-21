# AGENTS.md — deliverables

Template registry, format generators, the verification ladder, release.

## Depends on

`contracts`, `platform`.
Reached from `runtime` through the `doc.generate` tool, never imported by it.

## This package is the product's differentiator

A chatbot answers. Citadel produces a Word approval note with classification markings, an
approval block, inline citations, a hash and a provenance manifest. Everything else in the
repo exists to make this correct.

## Templates

Registry-driven, with declared placeholders. Formats: docx, xlsx, pptx, markdown, code.

Templates are **constructed** for v1 (ADR-0001 §Q11) — approval note, inspection summary,
calculation sheet. The risk in a constructed template is that it drifts toward whatever the
generator finds easy, which quietly redefines the target.

**So: fix the template first, by hand, as an artefact nobody may simplify. Then make the
generator match it.** A real organisational template must be able to replace a constructed
one with no code change; that property is the point, and it survives the templates being
invented.

## Verification — four tiers

Run before anything is shown as final. Report **per tier**, so a failure says which tier
failed and not merely that something did.

1. **Structural** — the template's declared sections are present and populated.
   Driven by the template's declaration. **Never by hardcoded headings**: the prototype
   checked for the literal strings `## Summary`, `## Maintenance History`, `## Sources`,
   which tied verification to exactly one document type.
2. **Schema** — placeholders filled, types correct, required fields non-empty.
3. **Citation** — every citation resolves to a real document, version, page and box.
4. **Grounding** — every quantitative claim traces to a cited block.

### Grounding is the hard one

"Every number traces to a source" is easy to say. It needs: numeric extraction from the
draft, matching against cited blocks, tolerance rules for derived values, and a defined
presentation for an unverifiable claim.

**An unverifiable claim is surfaced, not silently dropped and not silently passed.** The
user sees which number could not be traced. That is more useful than a document that is
quietly wrong, and it is the honest version of the grounding promise.

## Release

Approval is an explicit state transition, attached to deliverable release as an **option**,
not a mandatory gate on every task. Accept or reject with a comment; rejection returns one
bounded revision cycle; acceptance freezes, hashes and releases, with the whole chain —
who asked, what was retrieved, which model reasoned, what was computed, who approved — as
one immutable record.
