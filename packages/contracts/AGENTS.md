# AGENTS.md — contracts

The shared vocabulary. Domain types, envelopes, the event registry, the classification
lattice, state machines, signed decision receipts, and signed session tokens.

## The one rule

**Zero inward dependencies.** `citadel_contracts` imports nothing from any other Citadel
package, and nothing from a third-party library that is not stdlib, `pyjwt` or
`cryptography`. Checked from two vantage points, exactly as the prototype checked it — the
two files are not duplicates, neither substitutes for the other:

- `packages/contracts/tests/test_contracts_is_self_contained.py` — package-local, AST-based,
  provable with `citadel_contracts` as the *only* thing present (this package, pytest,
  pyjwt, cryptography — no rest of the repo). Ported from the prototype's own file of the
  same name and role.
- `tests/structural/test_contracts_import_boundary.py` — the repo-integrated counterpart,
  walking the real tree from root. Named differently from the package-local file on
  purpose — MONARCH's own `pyproject.toml` records a real pytest collection error from two
  test files sharing a basename, and giving every test file a globally unique basename is
  still how this repo avoids that, exactly as it did before `citadel_platform` existed. What
  changed is *how* the uniqueness is enforced: no `tests/` directory anywhere in this repo
  has an `__init__.py` (not this one either, not any more — see root `pyproject.toml`'s
  `--import-mode=importlib` comment for the second, package-name-level collision that
  forced that, once a second package's `tests/` existed to collide with this one on the
  bare name `tests`). The general layering rule for every package (not just this one) is
  `tests/structural/test_module_boundaries.py`, a separate file.

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

## Two signed token kinds, two keys

`receipts.py` (a decision receipt — key lifecycle deferred to M3-M4, per the porting
table above) and `identity.py` (a session token — PLAN-M0 task 7, key lifecycle in
`packages/platform/AGENTS.md`, needed now) are both Ed25519 via PyJWT, deliberately
shaped alike, and deliberately independent: distinct `TOKEN_TYPE` constants
(`"receipt"` vs `"session"`), distinct `SIGNING_ALGORITHM` constants (same value,
`"EdDSA"`, never a shared import), and distinct keys in production. A session-signing
key compromise must never let an attacker forge receipts, or vice versa — that
independence is enforced by never importing one module's constants into the other, not
by a comment asking someone not to. `identity.py`'s own module docstring explains where
it simplifies relative to `DecisionReceipt` (no nonce, no parallel timestamp
representation) and why each simplification is safe for what a session actually is.

## What goes here vs elsewhere

Here: the *shape* of a task, a document, a citation, a policy decision, a classification, a
receipt. Elsewhere: anything that touches a database, a model, a file or a socket.

A good test of whether something belongs here — can it be imported by a test that has no
database, no GPU and no network? If not, it goes in `platform`.
