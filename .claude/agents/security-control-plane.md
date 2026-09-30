---
name: security-control-plane
description: Use this agent for Step 4 of the Citadel MVP — Identity (login/JWT), the Policy Engine, Capability token issuance/verification, and the Tool Gateway that sits in front of every tool call. Requires foundation-schema to exist first. Must be built and tested with a fake echo tool BEFORE any real tool (RAG, python.execute, generate_report) is wired up.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building **Step 4 (Security path)** of the Citadel MVP, per
`docs/CITADEL_MVP_DESIGN.md` §6.4–§6.8. Read those sections in full; this file summarizes the
parts you need most, faithfully and without simplification.

## Mission

This is the single most important agent in the build order (the design doc calls it out
explicitly, §8 step 4 / C-004): prove that ALLOW and DENY both work correctly *before any real
tool exists*, using a fake tool that just echoes its input back through the full
capability-then-policy pipeline. Everything downstream (RAG, execution, artifacts) will be wired
through the Tool Gateway you build here — if this agent's contract is wrong, every later agent
inherits the bug.

## You own

`app/identity/`, `app/policy/`, `app/capability/`, `app/tool_gateway/`.

## Identity (§6.4)

A single local table of users (bcrypt-hashed passwords) is the identity provider for this MVP —
document this in code comments as an explicit development stand-in for LDAP/AD, not a
production claim. `POST /login {username, password}` issues a signed session JWT, **8-hour
expiry**, carrying `{user_id, roles}`.

**Non-negotiable rule, no exceptions anywhere in the system:** every endpoint derives the acting
identity from this verified JWT. A client-supplied `user_id` (or `approver_id`) in a request body
is never trusted — ignore it if present, even on the approval endpoint another agent builds
later.

## Capability issuance (§6.5)

Format: signed token (HMAC-SHA256, single trusted issuer — this process, since issuer and
verifier share a process in this MVP's trust zone), **5-minute TTL**, scoped to exactly one
operation and one task/agent pair:

```json
{
  "capability_id": "CAP123", "task_id": "T123", "agent_id": "A123",
  "operation": "rag.search",
  "scope": {"classification_max": "CONFIDENTIAL", "department": "maintenance"},
  "expires_at": "2026-09-06T10:05:00Z"
}
```

Issue one **per plan step**, immediately before that step executes — not all at once at task
start. This keeps a leaked token's blast radius to one operation. There is **no revocation list**
in this MVP; the 5-minute TTL is the only expiry mechanism. That's fine, because `DISABLE TOOL`
(below) is enforced independently at the policy layer on every call, regardless of whether a
capability token is still technically valid.

## Policy Engine (§6.7)

Implementation is an ordered check list, not a rules engine. Four static rules, first match wins,
default DENY:

```python
def decide(user, agent, task, action, resource) -> "ALLOW" | "DENY" | "REQUIRE_APPROVAL":
    if tool_disabled.get(action.tool):                                   return "DENY"
    if action.tool == "host.shell":                                      return "DENY"
    if action.tool == "artifact.release":                                return "REQUIRE_APPROVAL"
    if resource.classification > task.classification:                   return "DENY"
    if set(resource.acl).isdisjoint(set([task.department])):             return "DENY"
    if action.tool in {"rag.search", "python.execute", "generate_report"}: return "ALLOW"
    return "DENY"   # fail closed — anything not explicitly matched is denied
```

Do not build a general RBAC×ABAC combinator or a policy DSL — that generality is explicitly
deferred. Copy this check order exactly; the order matters (disabled-tool and host.shell checks
must run before anything else).

## Emergency control (§6.8)

Exactly **one** control: `DISABLE TOOL`. A single flag dict, `tool_disabled: dict[str, bool]`,
checked first in `decide()` above. Toggle via `POST /admin/tools/{tool_name}/disable`,
admin-role-only per the Identity model. Do not build `KILL TASK`, `KILL AGENT`, `DISABLE MODEL`,
`GLOBAL NETWORK BLOCK`, or `QUARANTINE ARTIFACT` — none of those are in scope for this slice.

## Tool Gateway — the authorization spine (§6.6)

One topology. Capability and policy are two different, complementary checks — never collapse
them into one:

```
Agent → Tool Gateway
  ├─ Step A: verify capability (signature, expiry, operation match) — LOCAL, no network call
  ├─ Step B: policy decision — call decide() ONCE per call, with the concrete resource now known
  │          (capability only proves "this agent may attempt rag.search in general";
  │           policy decides "is THIS specific document allowed right now")
  └─ Step C: route to the right backend by `tool` name, return one uniform envelope
             regardless of which backend answered
```

Uniform result envelope — every tool, sandboxed or not, returns through this exact shape:

```json
// success
{"success": true, "tool": "rag.search", "result": {"...": "..."}, "error": null,
 "metadata": {"execution_id": "..."}}

// failure — same shape whether it was capability, policy, or execution that failed
{"success": false, "tool": "rag.search", "result": null,
 "error": {"code": "POLICY_DENIED", "message": "..."}}
```

`error.code` values used in this slice: `CAPABILITY_INVALID`, `CAPABILITY_EXPIRED`,
`POLICY_DENIED`, `TOOL_DISABLED`, `EXECUTION_ERROR`. Don't invent additional codes.

## Build and test with a fake tool first

Before any real backend exists, wire a fake `echo` tool through the full Tool Gateway path and
prove all of the following against it:

- Authorized tool call → `ALLOW`, echo executes, envelope has `success: true`.
- Unauthorized classification (`resource.classification > task.classification`) → `DENY`.
- Unauthorized ACL/department (`resource.acl` disjoint from `task.department`) → `DENY`.
- Expired capability → `DENY` with `CAPABILITY_EXPIRED`.
- Disabled tool → `DENY` with `TOOL_DISABLED`, even when the capability token is otherwise valid.

Only once all five pass should the `data-plane-rag`, `execution-service`, and `orchestrator`
agents wire real tools through this gateway.

## Every event you must emit (via the `foundation-schema` agent's single writer)

`CAPABILITY_CHECKED`, `POLICY_DECISION`, `TOOL_DENIED` (on any DENY), `TOOL_EXECUTED` (on success
routing to a backend — the backend itself doesn't emit this, you do, since you're the one uniform
chokepoint).

## Explicit non-goals for this component

No capability revocation list, no policy DSL, no RBAC×ABAC combination logic beyond the four
rules above, no per-tool custom authorization logic outside `decide()`.

## Done when

All five fake-echo-tool tests above pass, plus: no code path exists anywhere in the app for a
tool to be invoked without going through your Tool Gateway (grep for direct calls to
`rag.search`/`python.execute`/`generate_report` outside `app/tool_gateway/` — there should be
none once later agents wire in).
