# Citadel — Target Architecture

**What this is:** the shape Citadel is being built toward, derived from the system's own purpose
and threat model. It is written without reference to what the repository currently contains.
**How to use it:** this document defines the destination. `CITADEL_MVP_DESIGN.md` describes the
vertical slice that exists today. Where they differ, **this document is the target and the code is
what changes.** Nothing here is negotiated down to fit the current package layout.

---

## 1. The invariant

Everything in this architecture exists to make one sentence true, and any change that weakens it is
rejected regardless of what it costs to avoid:

> **Every privileged operation is decided by the Control Plane, proven by a signed receipt that the
> executing service verifies for itself, and recorded in a chain no participant can rewrite.**

Three properties, each load-bearing:

| Property | What it rules out |
|---|---|
| **Decided centrally** | A component answering its own authorization question |
| **Proven cryptographically** | A component being trusted because of where it runs |
| **Recorded immutably** | A component that misbehaved being able to hide it |

The second is the one the current slice does not have, and it is the one that matters most. Today's
design trusts the Tool Gateway because it sits in the same process as the agent loop and is assumed
to be honest. **Process co-location is not a security property.** In the target, no service trusts
its caller's word about anything — it verifies a signature or it refuses.

This is the operational form of the project's founding principle: *the model is not the security
boundary; the LLM is an untrusted reasoning component and deterministic infrastructure enforces
control.* The same logic extends one step further in the target — **the orchestrator is not the
security boundary either.** It is the component that holds model output, so it inherits the model's
untrustworthiness.

---

## 2. The Decision Receipt

The mechanism that makes the invariant real. This is the piece the black-box audit found missing
(BB-015, BB-020, C-002): capability tokens were treated as a solved primitive and never specified,
and two documents described incompatible authorization topologies. This specifies it.

**Two checks remain two checks**, exactly as C-002 resolved — the receipt makes the second one
portable across a process boundary rather than collapsing it into the first:

| | Capability | Decision Receipt |
|---|---|---|
| Answers | "may this agent attempt `rag.search` at all?" | "is **this** call, on **this** resource, allowed right now?" |
| Minted | once per plan step | once per tool call |
| Lifetime | minutes | seconds |
| Bound to | task + agent + operation | task + agent + operation + **resource digest** + nonce |
| Verified by | the caller, locally | **the executing service**, before it acts |

```jsonc
// Decision Receipt — signed Ed25519 by the Control Plane, which alone holds the private key
{
  "decision_id":     "DEC-01J8...",
  "task_id":         "T123",
  "agent_id":        "A123",
  "operation":       "rag.search",
  "resource_digest": "sha256:9c1b...",   // over {resource_id, type, classification, acl}
  "scope":           {"classification_max": "CONFIDENTIAL", "department": "maintenance"},
  "rule":            "tool_in_allow_list",
  "issued_at":       "2026-09-21T10:00:00Z",
  "expires_at":      "2026-09-21T10:00:30Z",
  "nonce":           "5f3a..."
}
```

**A receipt is only ever issued for an ALLOW.** A denial returns an error envelope and no token —
there is nothing to replay, and a denial is still a first-class recorded outcome.

**Every executing service verifies, itself, before acting:** signature against the Control Plane's
public key; not expired; `operation` matches what it was asked to do; `resource_digest` recomputes
to match the resource it is *actually about to touch*; `nonce` unseen. Any failure is a refusal.

The digest binding is the point. An orchestrator that obtains a legitimate ALLOW for document A
cannot use it to fetch document B — the digest will not recompute. No amount of compromise in the
requesting component produces a receipt for a resource the Control Plane did not approve.

**What this makes true, and it is worth stating plainly:** once receipts are enforced, *where the
orchestrator runs stops mattering to the security model.* The "Control Plane shares a process with
the Orchestrator" limitation does not need to be fixed by moving code — it dissolves, because the
trust it depended on is gone.

---

## 3. Services

Five services. Each is defined by what it owns and, more importantly, by what it refuses.

| Service | Owns | Refuses |
|---|---|---|
| **Control** | Identity, policy evaluation, capability issuance, approval decisions, the audit chain, all private keys | Issuing a receipt without evaluating policy. Executing any tool. Ever. |
| **Orchestrator** | Task lifecycle, planning, the agent loop, revision. Holds model output. | Holding any private key. Deciding policy. Writing to the event chain directly. |
| **Data** | Documents, evidence, working memory, provenance, artifacts. Filters by ACL and classification *before ranking*. | Any read or write without a valid receipt whose digest matches the resource. |
| **Execution** | One-shot sandboxes. The only Docker socket in the system. | Any execution without a valid receipt. Any network route out. |
| **Models** | The model manifest, selection, local inference. | Serving a model whose ceiling is below the receipt's classification scope. |

**The Tool Gateway is not a service.** In the target it is a *client library* that the Orchestrator
uses: it requests a decision, receives a receipt, and carries it to the executing service. It is no
longer a trusted chokepoint, because there is nothing left to trust it with — a compromised gateway
can only fail to ask, never fake an answer. Making the gateway honest stops being a requirement.

**Approval lives in Control**, with the artifact and task state transitions it commits, because the
accountability chain and the audit chain must move together or neither is trustworthy.

**Working memory lives in Data**, under the same receipt rule as every other read and write. A
memory mutation is a privileged operation, not a side effect of a model call.

---

## 4. Repository shape

The services above are the units of deployment, so they are the units of the repository. There is
no top-level application package that contains the others as subfolders — that shape is what makes
a single-process assumption easy to keep making by accident.

```
citadel/
├── contracts/            the shared language, and the ONLY thing services share
│     envelopes · receipts · event types · state machines
│     classification lattice · domain schemas
├── services/
│     control/            identity · policy · capability · approval · audit · keys
│     orchestrator/       query router · planner · agent loop · revision
│     data/               rag · working memory · provenance · artifacts
│     execution/          the sandbox — the only Docker socket
│     models/             manifest · routing · local inference
├── clients/
│     cli/
│     console/            the browser workbench
├── deploy/               one Dockerfile per service · compose · runners
└── tests/
      contract/           each service against contracts/, in isolation
      integration/        the three demo paths, across real process boundaries
      invariants/         the structural tests that make the boundaries provable
```

**The one rule that keeps this honest: no service imports another service.** Shared meaning lives
in `contracts/` and nowhere else. A service that needs something from another service calls it over
HTTP with a receipt, or it does not get it.

This rule is what the current layout cannot express. `app/policy/` and `app/orchestrator/` sharing a
package means any future change can quietly reach across the boundary and nothing will fail. Under
`contracts/`, that reach does not typecheck.

**`contracts/` is the keystone, and it is written first.** It is the only part of this architecture
that every phase depends on, and the only part that is expensive to get wrong later.

---

## 5. Rules that hold at every phase

1. **Fail closed.** An unmatched policy check denies. An unverifiable receipt refuses. An unknown
   classification is not comparable and therefore not permitted. No silent passes, anywhere.
2. **Identity is derived, never accepted.** Every acting identity comes from a verified token. A
   `user_id` in a request body is ignored, on every endpoint, with no exception.
3. **Filter inside the data plane.** Authorization filtering happens before ranking and before an
   evidence object exists. A record the caller may not see never enters the candidate set.
4. **One writer to the chain.** The audit chain has exactly one owning service. Every other service
   contributes by asking it, and none can reorder or rewrite.
5. **Boundaries are proven by tests, not by documentation.** Every claimed boundary has a structural
   test that fails if the boundary is crossed, and every such test has a negative control proving it
   can fail.
6. **The three demo paths pass at the end of every phase.** Happy path, ACL denial, kill-switch.
   This is the project's own definition of done and it does not get suspended for a refactor.

Rule 6 is not a concession to the existing code. It is the one thing that keeps this architecture
answerable to reality instead of to a diagram.

---

## 6. Migration

Five phases. The ordering is derived from the invariant: establish the property first, then let the
topology catch up to it.

| Phase | What changes | Why here |
|---|---|---|
| **P1 — Authorization spine** | `contracts/` is written. Control issues signed Decision Receipts. Every executing backend verifies one before acting. Still one process. | The security property is established *before* anything moves. After this, moving code cannot weaken anything, because nothing depends on where code lives. |
| **P2 — Dissolve into services** | `app/` stops existing. Its contents become `services/*`, each independently deployable, each importing only `contracts/`. | Now mechanical and safe: every service already refuses un-receipted work, so crossing a process boundary changes nothing about its posture. |
| **P3 — Data Plane depth** | Working memory (Monarch, hardened) enters `services/data/` with classification and ACL in its schema. Provenance pins versions. Retention policy defined. | Needs P1's receipt rule to be safe and P2's boundary to be clean. |
| **P4 — Identity and policy maturity** | Real identity provider. Policy rules become data rather than an ordered function. | Needed before more than a handful of users and one department. |
| **P5 — Scale and operations** | Multi-agent, concurrency, the full emergency-control set, network attribution, storage-level immutability. | Everything here assumes the boundaries of P2 are real. |

**Note what P1 buys that a process split alone would not.** Splitting processes first would produce
services that still trust each other's word and merely happen to run separately — the diagram would
look right and the invariant would still be false. Establishing the receipt first means the split,
when it happens, is a deployment change rather than a security change.

---

## 7. What this architecture deliberately does not become

Not a distributed system. One machine, five processes, is the target — ADR-007's reasoning holds
and scaling out is not a goal this architecture serves. Not a policy DSL: rules become data in P4,
not a language. Not a multi-agent framework: agent-to-agent delegation is a capability type in P5 if
it earns its place, not a foundation. Not Kubernetes, Kafka, or a service mesh. Not cloud anything —
the entire point is that it runs on the organisation's own hardware with no egress.

The discipline that produced the current slice — every omission named and traced rather than left
implicit — carries forward unchanged. This document raises the ceiling; it does not remove the floor.
