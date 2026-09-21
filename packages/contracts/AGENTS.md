# AGENTS.md — contracts

The shared vocabulary. Domain types, envelopes, the event registry, the classification
lattice, state machines, and signed decision receipts.

## The one rule

**Zero inward dependencies.** `citadel_contracts` imports nothing from any other Citadel
package, and nothing from a third-party library that is not stdlib, `pyjwt` or
`cryptography`. Checked from two vantage points, exactly as the prototype checked it — the
two files are not duplicates, neither substitutes for the other:

- `packages/contracts/tests/test_contracts_is_self_contained.py` — package-local, AST-based,
  provable with `citadel_contracts` as the *only* thing present (this package, pytest,
  pyjwt, cryptography — no rest of the repo). Ported from the prototype's own file of the
  same name and role.
- `tests/structural/test_module_boundaries.py` — the repo-integrated counterpart, walking
  the real tree from root and wired into CI for every package's dependency rule, not just
  this one. Contracts' "zero inward" is the strictest case of the general layering rule
  every package follows; this is where that general check lives (Task 17 — not yet written).

If you need something from `platform` in here, the dependency is backwards: the type
belongs here and the behaviour belongs there.

## Port, don't rewrite

`AI_WORKBENCH/CITADEL/contracts/` is a genuinely good package and is the one place the
prototype's *code* is worth carrying over rather than just its ideas:

| File | Port as-is | Change on the way in |
|---|---|---|
| `classification.py` | ✅ lattice with explicit comparison; unknown markings denied | — |
| `receipts.py` | ✅ Ed25519, digest over `{resource_id, type, classification, acl}`, nonce, short TTL | Key material gets a documented lifecycle. A per-process random secret is a correct fail-closed *default* and an unusable *only option*. |
| `state_machines.py` | ✅ | — |
| `envelopes.py` | ✅ | — |
| `domain.py` | mostly | Drop `UNIQUE(task_id)`-style "one agent per task" assumptions. |
| `events.py` | the discipline, not the file | **The vocabulary becomes open.** Sixteen closed event types that reject anything new is the named failure mode. A registry with registration, not a closed `StrEnum`. |
| `tests/test_receipts.py` | ✅ all 13 KB of it | It is adversarial and it is the reason to trust the receipts. |

## What goes here vs elsewhere

Here: the *shape* of a task, a document, a citation, a policy decision, a classification, a
receipt. Elsewhere: anything that touches a database, a model, a file or a socket.

A good test of whether something belongs here — can it be imported by a test that has no
database, no GPU and no network? If not, it goes in `platform`.
