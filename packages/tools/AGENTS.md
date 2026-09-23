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

**Built.** `citadel_tools.policy.evaluate(rules, actor=, resource=, tool=, receipt=)
-> Decision` is the pure evaluator. `registry/roles.yaml` answers `actor.capabilities`,
and `actor_facts_for_task()` caps an actor's clearance at the task's classification.
`Chokepoint.invoke(ctx, name, arguments)` is the one function every tool call goes through:

1. Resolve the tool in `registry/tools.yaml`, whose plugin is found through its
   package's `PLUGINS` table.
2. Coerce the arguments, then validate them against the declared JSON schema.
3. Let the plugin describe the resource it would touch.
4. Evaluate policy with `receipt.valid=True`, the question being "would this be allowed
   with a valid receipt".
5. Audit the decision, allow and deny alike.
6. On allow, sign a single-use receipt bound to the resource digest (`receipt.issued`).
7. Run the plugin.

The executing boundary verifies that receipt itself: `DataBoundary` for documents,
workspace and deliverables, and `sandbox.run_verified` for code, each with its own nonce
store. If the receipt fails there, the decision is re-evaluated with `valid=False`, which
yields `deny-missing-receipt` and is audited. A result whose boundary never verified is
discarded. `Chokepoint.available(ctx)` answers which tools this actor could use for this
task, and why not, without invoking anything; the planner uses it.

Outputs are harvested by shape, not by tool name. A `ToolOutput` carries `evidence`
(E# document regions, C# computations), `artifacts` and a summary for the model. The
agent loop never learns which tool produced what. `tests/structural/test_single_chokepoint.py`
keeps `execute_tool`/`dispatch_tool`/`TOOL_REGISTRY` from appearing anywhere else.

## The sandbox

The only component permitted to execute model-authored code. It is **one hardened service
on an internal-only network, not a container per run**
([ADR-0007](../../docs/adr/0007-the-sandbox-is-a-service-not-a-container-per-run.md)).
A container per run would need the worker to hold the Docker socket, which is a far larger
capability than the code it would contain. Every run gets a verified receipt, a fresh
directory, a separate process with rlimits and a process-group timeout, and an audit hook
as defence in depth. The container itself provides the boundary: no route out, read-only
root, `cap_drop: ALL`, and the api/worker rulesets refuse any connection it tries to open.
Without containers (`scripts/run.sh`), the same `run_verified` runs in-process as
`LocalSandboxRunner`, and every result from it says `kind: process`.
