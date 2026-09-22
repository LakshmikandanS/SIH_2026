# AGENTS.md — tools

Tool plugins and the policy chokepoint. The narrow waist of the whole system.

## Depends on

`contracts`, `platform`, `gateway`, `knowledge`, `deliverables`.

## The chokepoint

**One function. Every tool invocation passes through it. No exceptions.**

```
resolve tool from registry
  → validate arguments against the tool's declared JSON schema
  → evaluate policy (data-driven rule table, first match wins, default deny)
  → record the decision (allow AND deny — both are audit events)
  → dispatch
```

The executing side **verifies a receipt** rather than trusting the caller's word. That is
the change from the prototype: a compromised orchestrator is the actual threat, so the
sandbox and data boundaries check a signed receipt bound to a resource digest rather than
believing what they were handed.

Two independent checks, deliberately not collapsed:

- **coarse:** may this actor attempt this operation at all?
- **fine:** is this specific resource allowed, right now?

Keeping them separate is what lets a kill-switch work against already-issued credentials.
Collapsing them is a plausible-looking simplification that removes that property.

## Tools are declarative plugins

Discovered from a manifest. Each declares: name, JSON schema for arguments, required
capabilities, side-effect class, classification ceiling.

**Adding a tool must never require editing the agent loop.** If it does, the loop is
dispatching on tool names — the named failure mode (`if/elif` on three tool names) that
made the prototype's loop unextensible.

## Initial set

| Tool | Notes |
|---|---|
| `docs.search`, `docs.read` | Corpus retrieval and full-document read |
| `fs.read`, `fs.write` | Scoped workspace filesystem |
| `code.run` | Sandboxed Python. `--network none`, resource-capped |
| `sheet.read`, `sheet.write` | Spreadsheet manipulation |
| `vision.extract` | Re-read a region. Normal extraction happens at ingest, not here |
| `doc.generate` | docx / xlsx / pptx from a template plus content |
| `calc.evaluate` | Arithmetic with retained, displayable working |

## Policy is data

`registry/policy.yaml`. First match wins, default deny. A new tool or a new department is a
registry edit, never a code change. The prototype's four hardcoded ordered rules in a
Python function had the right *semantics* and the wrong *location*.

**The evaluator (`citadel_tools.policy`, PLAN-M0 task 8) is built and tested against the
real registry — the chokepoint that calls it is not, yet.** `evaluate(rules, actor=,
resource=, tool=, receipt=) -> Decision` is a pure function: no I/O, no audit-chain write.
`resource` and `tool` are `citadel_contracts.domain.Resource` and
`citadel_platform.registry.schema.ToolEntry` directly — both already carry exactly the
fields the rules reference. `actor` is the one new shape (`ActorFacts`): `registry/
roles.yaml` is what answers `actor.capabilities` from a role, since `citadel_contracts.
domain.User` carries roles, not capabilities, and `actor_facts_from_user()` is the one
place a `User` becomes it (see that function's own docstring for the two normalisations —
singular role, uppercased clearance — done there and nowhere else). `test_policy.py` has
one test per real rule in `registry/policy.yaml`, in file order, plus an empty-rule-list
test for the default deny. What is still unbuilt: `TOOL_REGISTRY`, `execute_tool`/
`dispatch_tool`, JSON-schema argument validation, tool resolution and dispatch, and
turning a `Decision` into an audit event — the rest of the resolve → validate → policy →
record → dispatch chain above.

## The sandbox

One-shot containers. `--network none`, CPU/memory/PID capped, no host mounts, destroyed
after use. It is the only component permitted to execute model-authored code, and it runs
on the app box where it contends with nothing.
