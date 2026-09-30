# Workflow — what happens, and how

This traces **one real task**, from a human typing it to a released document, naming the actual
module at each hop. The event sequence below is not illustrative: it is copied from a real run.

If you want the *contract* rather than the walkthrough, read
[`CITADEL_MVP_DESIGN.md`](CITADEL_MVP_DESIGN.md). If you want to know why a given
implementation choice was made, read [`BUILD_LOG.md`](BUILD_LOG.md).

---

## 0. The two processes

Citadel runs as **two OS processes**, and the split is the security model, not a deployment
detail.

| Process | Holds | Runs |
|---|---|---|
| **Trusted workflow zone** — `app.main` | The database, the vector store, the signing keys | Citadel's own code only. Never executes model-authored content |
| **Isolated execution zone** — `execution_service` | **The only Docker socket in the system** | Spawns throwaway containers that run model-written code |

The trusted zone reaches the execution zone over HTTP and **imports no Docker library at all** —
there is a test that greps the source tree to keep it that way. A compromised trusted zone still
cannot reach the Docker socket directly, because it has no code path to it.

---

## 1. Sign in

`POST /login` → `app/identity/service.py`

The password is checked against a bcrypt hash and a **session JWT** is issued, valid 8 hours,
carrying `{user_id, roles}`.

From this point, **every endpoint derives who you are from that verified token**. A `user_id` or
`approver_id` in a request body is ignored. This is not enforced by remembering to check it —
`SessionIdentity` has no constructor path from a request body, so the wrong thing is unbuildable.

---

## 2. Submit a task

`POST /task` → `app/orchestrator/router.py`

The Query Router creates the `Task` row and owns the `task_id`. Classification is **user-declared
and authoritative** — never inferred from the text. Then it hands off synchronously to the
Orchestrator (`POST /internal/orchestrate`, same process, a plain function call).

> `EVT01  TASK_CREATED`
> `EVT02  STATE_COMMITTED`
> `EVT03  AGENT_STARTED`

---

## 3. Plan

`app/orchestrator/plan.py` → `app/model_router/`

The Model Router picks a model from a **static lookup table** — not a scoring formula. A task whose
classification exceeds the model's ceiling fails routing outright with
`MODEL_CLASSIFICATION_INCOMPATIBLE`.

The reasoning model (`hermes3`) is asked once, with `format=json` and a JSON Schema, for a 3-step
plan: `rag.search` → `python.execute` → `generate_report`. If the output fails schema validation,
**exactly one** repair prompt is sent; a second failure marks the task `FAILED`.

> `EVT04  PLAN_CREATED`

### The one thing to know about planning

The model returns a plan whose `python.execute` step contains **hallucinated code** — in testing
it reliably produced `from search_engine import search`, which does not exist. This is expected:
the design specifies that argument as *"computed at runtime from S1's evidence."*

**That code is never executed.** `app/orchestrator/think.py::_think_python_execute` discards it
entirely and builds real code from the evidence actually retrieved. The planner decides *what to
do*; THINK decides *the concrete arguments*. Anything that later touches planning must not start
trusting the planner's literal `code`.

---

## 4. The agent loop

`app/orchestrator/agent_loop.py`

A deterministic loop, not a framework. Per plan step:

```
THINK        → given the step's action + evidence so far, decide concrete arguments
ACTION       → emit exactly one structured action
OBSERVATION  → receive the Tool Gateway's result envelope
DECISION     → CONTINUE | RETRY (≤1) | TERMINATE
```

Terminates on `SUCCESS`, `FAILED`, `MAX_STEPS` (default 6), or `WAITING_FOR_APPROVAL`.

**The agent never calls a tool.** Every action goes through the Tool Gateway, and the agent never
sees a capability token it was not issued for that specific step.

---

## 5. Every tool call — the authorization spine

`app/tool_gateway/gateway.py::invoke`

This is the single chokepoint. Nothing reaches a tool any other way.

Immediately before each step, `app/capability/service.py::issue_for_step` mints **one** capability
— signed HMAC-SHA256, scoped to one operation and one task/agent pair, **5-minute TTL**. One per
step, not all at task start, so a leaked token's blast radius is one operation.

```
Step A — verify the capability      signature, expiry, operation match. Local. No round-trip.
Step B — policy decision            the concrete resource is known only now
Step C — route to the backend       one uniform envelope, whichever backend answered
```

> `EVT06  ACTION_REQUESTED`
> `EVT07  CAPABILITY_CHECKED`
> `EVT08  POLICY_DECISION      decision=ALLOW`

### Why two checks and not one

The capability proves *"this agent may attempt `rag.search` in general."* Policy decides *"is
**this** document allowed, right now."* The gateway cannot answer the second question at issue
time, because the concrete target is not known until the step runs.

Collapsing them would break central revocation — see §9.

### The policy engine

`app/policy/engine.py` — four ordered rules, **first match wins, default DENY**:

```python
if tool_disabled.get(action.tool):                     return "DENY"            # kill-switch first
if action.tool == "host.shell":                        return "DENY"
if action.tool == "artifact.release":                  return "REQUIRE_APPROVAL"
if resource.classification > task.classification:      return "DENY"
if set(resource.acl).isdisjoint({task.department}):    return "DENY"
if action.tool in ALLOWED_TOOLS:                       return "ALLOW"
return "DENY"                                          # fail closed
```

The order is load-bearing: the disabled-tool check runs **before** anything else, which is what
makes the kill-switch beat a valid token.

`invoke()` **never raises** for an authorization or execution failure. It returns an envelope.
"Denied" is a first-class outcome, not an exception — that is why a denial appears in the trace
as a normal recorded event rather than a crash.

---

## 6. Retrieval — `rag.search`

`app/rag/search.py`

The requester block comes from the **verified capability's scope**, never from agent-supplied
arguments. Then, for every chunk:

```python
ok, reason = _passes(chunk, requester)   # classification, then ACL
if not ok:
    denied_by_document.setdefault(...)   # recorded by id and marking only — never its text
    continue                             # never scored, never becomes Evidence
```

**Filtering happens before ranking, and before an `Evidence` object exists.** A document you may
not see is not returned and then dropped — it never enters the candidate list. The classification
comparison uses the ordered lattice (`Classification.exceeds`), never a string compare, and fails
closed on an unknown marking.

> `EVT09  EVIDENCE_RETRIEVED   filtered_documents: [DOC-FIN-Q3]`
> `EVT10  TOOL_EXECUTED`

That `filtered_documents` field is the denial path made visible: the finance document was
present, considered, and withheld.

---

## 7. Sandboxed compute — `python.execute`

`app/execution/backend.py` → HTTP → `execution_service/sandbox.py`

The trusted zone makes an HTTP call. The Execution Service spawns **one** container:

```python
network_disabled=True          # no network interface exists inside it
read_only=True                 # root filesystem is read-only
tmpfs={"/tmp": ...}            # scratch is in memory, never touches the host disk
cap_drop=["ALL"]
security_opt=["no-new-privileges"]
mem_limit / nano_cpus / pids_limit / wall-clock timeout
# no volumes, no ports, no privileged, no docker socket
```

Code is passed as the container's command, so there is no host mount at all. The container is
force-removed in a `finally` — success, failure, or timeout.

A timeout or OOM kill is an **infrastructure failure** (`EXECUTION_ERROR`). A nonzero exit from
the caller's own code is **data**, returned normally — those are different things and are not
conflated.

> `EVT14  TOOL_EXECUTED`

**The sovereignty claim is a test, not a sentence.** `tests/test_execution.py` attempts a real
outbound call — HTTPS and a raw TCP socket — from inside the container and requires both to fail.

---

## 8. Generate, verify, approve, release

`app/artifact/` → `app/approval/router.py`

`generate_report` renders the one template (`maintenance_summary_v1` — "selection" is a no-op,
there is exactly one) from the retrieved evidence and the computed figures.

The **Verifier** then runs synchronously — a plain function, not a service, not an LLM judge:

```python
file_exists_and_readable()
compute_and_store_sha256()
has_required_sections(["Summary", "Maintenance History", "Sources"])
all(evidence.classification <= task.classification)
len(provenance) > 0
```

Any failure marks the task `FAILED`. There is no auto-revision on a verification failure.

> `EVT19  ARTIFACT_CREATED`
> `EVT20  ARTIFACT_VERIFIED`
> `EVT21  APPROVAL_REQUESTED`

The task now sits at `WAITING_FOR_APPROVAL`. Nothing is released.

### The human gate

`POST /approvals/{approval_id}/decision` — the `approver_id` comes from the session JWT, never the
body, and the endpoint requires the `approver` role. The engineer who submitted the work **cannot
release it**.

On `APPROVED`, in **one transaction**: `Approval=APPROVED`, `Artifact=RELEASED`,
`Task=COMPLETED`, and both events emitted.

> `APPROVAL_GRANTED`
> `ARTIFACT_RELEASED`

On `REJECTED`, the task enters the one scoped revision (§5.3): completed steps' evidence is kept,
only the report is discarded, and `generate_report` re-runs **once** with the approver's comment.
A second rejection ends the task `FAILED`. There is no general replanning.

Once `RELEASED`, every mutating endpoint refuses the artifact — enforced at the API layer, with
the state machine treating `RELEASED` as terminal as a backstop.

---

## 9. The kill-switch

`POST /admin/tools/{tool}/disable` — admin role only.

It sets one flag, and `decide()` checks that flag **first**, on every call. An agent holding a
capability that is still valid and unexpired is denied on its very next call:

> `CAPABILITY_CHECKED   valid, unexpired`
> `POLICY_DECISION      DENY — tool disabled by administrator`
> `TOOL_DENIED`

This is the payoff of keeping capability and policy separate. Nothing had to be revoked, no token
had to be hunted down, and there is no revocation list in this slice at all — the policy layer
simply answers differently.

**It is one-way.** There is no re-enable endpoint — §6.8 names exactly one control, and
`app/policy/router.py` says so in its own docstring. The endpoint ignores any `disabled` field in
the body and always disables. The flag lives in the server process, so a restart clears it.

The agent's own view of this is a normal failure, not a crash:

```
status : FAILED
reason : step S2 (python.execute) failed after 2 attempt(s):
         TOOL_DISABLED tool 'python.execute' disabled by administrator
```

Note "2 attempts": the agent loop's one permitted retry ran and was denied again, which is the
correct behaviour — a retry does not get a second opinion from policy.

---

## 10. The audit chain

`app/observability/writer.py`

**One function — `append_event` — is the only code path that writes the event table.** Not a
convention: `tests/test_audit.py` greps `app/` and fails if any other module constructs an
`Event`, and it is checked against a negative control so the test cannot silently rot.

Each event's hash covers `{event_id, task_id, actor_id, event_type, payload, timestamp}` plus the
previous hash. `event_type` is deliberately inside the preimage: without it, relabelling a
`TOOL_DENIED` as a `TOOL_EXECUTED` would not break the chain — and the denial and kill-switch
demos rest entirely on that field being trustworthy.

Ordering uses an integer `seq`, never the display id, so traces stay correct past `EVT9999`.

`/trace` replays the whole thing. A denial appears in exactly the same view as a success, which is
the point: governance you can see, not governance you are asked to believe.

---

## The full sequence, for reference

From a real run, 23 events to `WAITING_FOR_APPROVAL`, then 2 more on approval:

```
01  TASK_CREATED          human submitted it
02  STATE_COMMITTED
03  AGENT_STARTED
04  PLAN_CREATED          hermes3, schema-validated
05  STATE_COMMITTED
06  ACTION_REQUESTED      step 1 — rag.search
07  CAPABILITY_CHECKED    minted for this step only
08  POLICY_DECISION       ALLOW
09  EVIDENCE_RETRIEVED    5 results · filtered_documents: [DOC-FIN-Q3]
10  TOOL_EXECUTED
11  ACTION_REQUESTED      step 2 — python.execute
12  CAPABILITY_CHECKED
13  POLICY_DECISION       ALLOW
14  TOOL_EXECUTED         ~2.5s — a real container ran
15  ACTION_REQUESTED      step 3 — generate_report
16  CAPABILITY_CHECKED
17  POLICY_DECISION       ALLOW
18  TOOL_EXECUTED
19  ARTIFACT_CREATED
20  ARTIFACT_VERIFIED     5/5 structural checks
21  APPROVAL_REQUESTED    → WAITING_FOR_APPROVAL
22  STATE_COMMITTED
23  STATE_COMMITTED
--- human approves ---
24  APPROVAL_GRANTED      approver_id from the session, never the body
25  ARTIFACT_RELEASED     one transaction with the two state moves
```

Note events 10 → 11: **no `POLICY_DECISION` is skipped, but a failed Step A skips Step B.** An
expired capability produces `CAPABILITY_CHECKED` then `TOOL_DENIED` with nothing between them.
Do not assume the three always arrive as a triple.
