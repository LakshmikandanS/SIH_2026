# AGENTS.md — structural tests

Tests that fail when an architectural boundary is crossed. The single best engineering
habit inherited from the prototype, and the only thing that keeps the invariants in the
root `AGENTS.md` from becoming aspirational comments.

## Every detector ships with a negative control

This is the rule that makes the whole idea work.

A grep-based test that finds nothing passes. It also passes when the pattern is wrong, when
the path is wrong, when the file glob stopped matching after a refactor, and when someone
deleted the body. A test that cannot fail is worse than no test, because it produces
confidence.

So every detector is paired with a fixture that it **must** catch:

```
tests/structural/
  test_no_hardcoded_models.py
  fixtures/negative_controls/hardcoded_model.py.txt     # detector must flag this
```

The test asserts both directions: clean tree → no findings, **and** control fixture →
exactly one finding. If the control stops being caught, the suite fails, and you have
learned that your detector rotted before it mattered.

Controls live as `.txt` so they are never imported or collected by pytest.

## The detectors M0 owes

| Test | Invariant | Negative control |
|---|---|---|
| `test_contracts_is_self_contained` | `contracts` imports nothing internal | A module importing `citadel_platform` |
| `test_module_boundaries` | The layer rule in root `AGENTS.md` | `runtime` importing `knowledge` |
| `test_no_hardcoded_models` | No model id / VRAM / param count outside `registry/` | A literal model tag in a service |
| `test_inference_isolation` | Only `gateway` imports an inference client | An inference import in `runtime` |
| `test_identity_not_from_body` | Identity never read from a request body | A handler reading `body["user_id"]` |
| `test_no_string_classification_compare` | Classification compared via the lattice, never `<`/`>` on strings | `if doc.classification > user.max` |
| `test_single_chokepoint` | No tool dispatch outside the chokepoint | A direct tool call in `runtime` |
| `test_no_egress` | No outbound network call outside the allowed clients | A `requests.get("https://…")` |
| `test_no_external_urls_in_build` | Frontend assets reference no external host | An asset with a CDN URL |
| `test_profile_ceiling_enforced_at_ingest` | Ingest refuses a document above the active profile's `classification_ceiling` | An ingest path that defaults a missing classification |

## Write them as AST checks where you can

Grep is fine for a first pass and is honest about being a first pass. An AST walk catches
aliased imports and does not trip on the word appearing in a docstring. Where a detector is
grep-based, say so in its docstring so the next person knows its blind spots rather than
trusting it further than it deserves.

## When a detector fires on legitimate code

**Do not add an exception list.** An allowlist that grows is how the boundary dissolves one
justified entry at a time. Either the code is in the wrong package, or the invariant was
wrong and needs a superseding ADR. Both of those are real outcomes; a `# noqa` is not.
