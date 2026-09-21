# AGENTS.md — contracts

The shared vocabulary. Domain types, envelopes, the event registry, the classification
lattice, state machines, and signed decision receipts.

## The one rule

**Zero inward dependencies.** `citadel_contracts` imports nothing from any other Citadel
package, and nothing from a third-party library that is not stdlib or `cryptography`.
This is enforced by `tests/structural/test_contracts_is_self_contained.py`, ported from the
prototype, which is the reason the prototype's own `contracts/` ports cleanly to here.

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
