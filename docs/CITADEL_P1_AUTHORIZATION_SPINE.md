# Citadel P1 — The Authorization Spine

**Purpose:** make `CITADEL_TARGET_ARCHITECTURE.md` §1's invariant true. After this phase, no
component in Citadel acts on a privileged operation because it trusts its caller — it acts because
it verified a signature, or it refuses.
**Status:** frozen contract for this phase.
**Precedence:** `CITADEL_TARGET_ARCHITECTURE.md` is the destination. This document is the first
increment toward it and supersedes `CITADEL_MVP_DESIGN.md` for the authorization path specifically
(§6.5, §6.6, §6.7's call path). Everything else in the MVP contract still holds.

---

## 0. Why this first, and not the process split

The obvious first move is to pull the Control Plane into its own process, since that is the
limitation the MVP doc names. **That is the wrong order**, and the reason is worth stating because
it is the whole logic of this phase.

Splitting processes first produces services that still trust each other's word and merely happen to
run separately. The deployment diagram would look correct while the invariant stayed false: a
compromised orchestrator would simply call a backend directly, and no backend would be able to tell.
The split would buy a diagram.

Establishing the receipt first inverts that. Once every executing service verifies a signed decision
before acting, *where a component runs stops being a security property at all* — and the process
split becomes a deployment change rather than a security change. The property comes first; the
topology catches up in P2.

There is a second reason this ordering is right: **the Execution Service is already a separate
process.** P1 can therefore prove the receipt mechanism across a real process boundary, using a
boundary that already exists, before relying on it for four more.

---

## 1. `contracts/` — written first

A new top-level package. It is the only thing services will ever share (target §4), so it is built
before anything consumes it, and it has its own tests that depend on nothing else.

```
contracts/
├── receipts.py       DecisionReceipt, issue signature verification, canonical digest
├── envelopes.py      the uniform tool result envelope, the closed error-code set
├── events.py         the closed event-type vocabulary
├── classification.py the ordered lattice + `exceeds`, fail-closed on unknown markings
├── domain.py         Task, Agent, Evidence, Artifact, Approval, User as plain schemas
└── state_machines.py the three state machines and their mapping
```

**`contracts/` imports nothing from the rest of the repository, ever.** A structural test enforces
this from the first commit — it is the rule that makes P2 possible, and it is cheapest to hold from
the beginning.

Moving the existing definitions here is not a rename for tidiness. Today `app/db/state_machines.py`
and `app/policy/tools.py` are reachable only by importing the application package; under the target,
a service that needs the classification lattice must not have to import the orchestrator to get it.

### 1.1 The canonical resource digest — specify exactly, or two services will disagree

```python
def resource_digest(resource) -> str:
    payload = {
        "resource_id":    resource.resource_id,
        "type":           resource.type,
        "classification": resource.classification,
        "acl":            sorted(resource.acl),        # order-independent
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

Sorted keys, no whitespace, sorted ACL, UTF-8. This function lives in `contracts/receipts.py` and is
the only implementation — the issuer and every verifier call the same code. A digest computed two
ways is a digest that fails in production and passes in tests.

**On granularity:** the digest binds *agreement between what the Control Plane approved and what the
executing service is about to do*, not a single database row. For `rag.search` the descriptor is
scope-shaped (classification ceiling and department) and the Data Plane filters against it; for a
single-document read it names that document. Either way, if the descriptor presented to the executing
service differs by one character from the one the Control Plane approved, the digest does not
recompute and the call is refused.

---

## 2. Keys

Three Ed25519 keypairs, not one, and not shared secrets:

| Keypair | Private held by | Public held by | Signs |
|---|---|---|---|
| `session` | Control | everything | session tokens |
| `capability` | Control | Orchestrator | capability tokens |
| `receipt` | Control | every executing service | decision receipts |

Separate keypairs because a leaked receipt key must not also mint eight-hour sessions. This is the
same reasoning the current code already applies to its two HMAC secrets — carried to its conclusion.

The current HMAC scheme is removed, not deprecated. `app/config.py` justifies HMAC on the grounds
that issuer and verifier share a process; the target has no such grounds, and keeping a symmetric
key means every holder is a potential issuer. `CITADEL_SESSION_SECRET` and
`CITADEL_CAPABILITY_SECRET` are replaced by `*_PRIVATE_KEY` / `*_PUBLIC_KEY` pairs.

**Any component other than Control that is handed a private key must refuse to start.** Write this
check before anything else in the phase and let it fail; it is the executable statement of what P1
is for.

---

## 3. The issuing path

One module in the Control Plane is the only code in the system that signs a receipt, and it can only
do so as the direct result of a policy evaluation. The two are not separable — there is no function
that signs a receipt from arbitrary inputs.

```
decide_and_issue(user, agent, task, action, resource) -> PolicyOutcome | (PolicyOutcome, Receipt)
    outcome = evaluate(...)            # the existing ordered rule chain, unchanged
    if outcome.decision != ALLOW:
        return outcome                 # no token exists to leak, replay, or forge
    return outcome, sign(receipt_for(outcome, resource))
```

The rule chain itself does not change in this phase. Four ordered rules, first match wins, default
DENY, kill-switch checked first — §6.7 stands exactly as written. P1 changes what happens *after* a
decision, not how it is reached.

A `DENY` produces no receipt at all. There is nothing to replay and nothing to steal, and the denial
is still recorded as a first-class `TOOL_DENIED` event exactly as it is today.

---

## 4. The verifying path

Every executing service verifies, itself, immediately before it acts. Not at its edge, not in a
middleware it shares with its caller — in the function that is about to touch the resource.

```python
def verify_receipt(token, *, operation, resource, seen_nonces) -> Receipt   # raises on any failure
    #  1. signature against the receipt public key
    #  2. not expired  (receipts live seconds; single machine, so no skew allowance)
    #  3. receipt.operation == operation
    #  4. receipt.resource_digest == resource_digest(resource)   ← recomputed here, now
    #  5. receipt.nonce not in seen_nonces   → then record it, TTL = receipt lifetime
```

Check 4 is the one that matters. An orchestrator holding a legitimate ALLOW for one resource cannot
spend it on another, because the verifier recomputes the digest over the resource *it* received, not
the one the caller claims.

Verifiers in this phase: `rag.search` (Data Plane), the Execution Service (over HTTP, across the
existing process boundary), and artifact generation. Each keeps its own nonce set — a shared one
would be a shared trust assumption, which is the thing being removed.

---

## 5. What the Tool Gateway becomes

It stops being a trusted chokepoint and becomes a courier. Its job is now: verify the capability
(Step A, unchanged and still local), ask the Control Plane for a decision (Step B, which now returns
a receipt), and carry that receipt to the backend (Step C).

It is no longer trusted with anything, and that is the point — a compromised gateway can fail to ask,
but it cannot fabricate an answer. The MVP's requirement that the gateway be the only path to a tool
becomes a correctness convenience rather than a security control.

Its event emissions are unchanged: `CAPABILITY_CHECKED`, `POLICY_DECISION`, `TOOL_DENIED`,
`TOOL_EXECUTED`. The `POLICY_DECISION` payload gains the `decision_id`, so a receipt in a log can be
traced to the decision that produced it.

---

## 6. Work order

Eight steps. **The three demo paths pass at the end of every step** — happy path, ACL denial,
kill-switch (target §5, rule 6). Commit at each checkpoint.

| # | Step | Done when |
|---|---|---|
| 1 | Build `contracts/`: receipts, envelopes, events, classification, domain, state machines. Its own tests, importing nothing else. Add the structural test that `contracts/` imports nothing from the repo. | `contracts/` tests pass standalone; the rest of the repo still builds against it |
| 2 | Three Ed25519 keypairs replace the two HMAC secrets. Add the refuse-to-start-with-a-private-key check to every non-Control component. | Full suite green; sessions and capabilities work as before |
| 3 | Implement `decide_and_issue` as the sole signing path. Structural test: exactly one call site signs a receipt. | Receipts are issued on ALLOW, absent on DENY |
| 4 | Gateway carries the receipt to backends. No backend verifies yet. | Suite green; receipts flow end to end unused |
| 5 | `rag.search` verifies before filtering. | The denial path still denies — now by two independent mechanisms |
| 6 | The Execution Service verifies over HTTP, with its own public key and nonce set. | `python.execute` refuses a call with no receipt, a stale receipt, or a mismatched digest |
| 7 | Artifact generation verifies. | The happy path still releases; a tampered digest refuses |
| 8 | Invariant tests, negative controls, CI greps. | §7 in full |

Step 4 is deliberately a no-op checkpoint: receipts flowing but unverified proves the plumbing before
any refusal logic can mask a wiring bug.

---

## 7. Invariants

Each is a structural test in `tests/invariants/`, and **each ships with a negative control** — a
deliberately violating fixture the test is asserted to catch. A structural test that cannot fail is
worse than none, because it reads as protection.

1. `contracts/` imports nothing from `services/`, `app/`, `cli/`, or `execution_service/`.
2. Exactly one module signs a decision receipt, and it does so only in `decide_and_issue`.
3. No component other than Control reads a `*_PRIVATE_KEY`, and no JWT `encode` call exists outside
   Control. Verification is unrestricted.
4. Every registered tool backend calls `verify_receipt` before touching its resource. A backend added
   later without one fails this test.

Add the matching grep pre-flights to CI beside the existing "trusted zone must not import docker"
step, in the same wording and the same `::error::` style.

---

## 8. Definition of done

The three demo paths behave exactly as they do today, from the user's side. Underneath, every one of
them is now carrying signed receipts that the executing services verify independently.

Then the checks no test states as well as a person can:

- Take a valid receipt for the maintenance document and replay it against the finance document. It
  must refuse on the digest, not on the ACL — proving the binding works and not merely that the old
  filter still runs.
- Replay a receipt twice. The second must refuse on the nonce.
- Hold a receipt for forty seconds, then use it. It must refuse on expiry.
- Register a tool backend that skips verification. Invariant 4 must fail.

If any of those four succeeds, the spine is not where this document says it is.

---

## 9. Not in this phase

The process split (P2). Working memory (P3). A real identity provider or data-driven policy rules
(P4). Multi-agent, concurrency, the remaining emergency controls, storage-level immutability (P5).
Receipts for model calls — the Model Router's classification ceiling stays a local check until P2
gives it a boundary worth enforcing across.
