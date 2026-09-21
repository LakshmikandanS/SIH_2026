# AGENTS.md — services

The deployable processes. These may import any package; nothing imports them.

| Service | Runs | Notes |
|---|---|---|
| `api` | App box | FastAPI. Auth, task submit/cancel, SSE streams, artifacts, admin |
| `worker` | App box | Pulls from the queue, runs the agent loop, writes the journal |
| `sandbox` | App box | One-shot container image. `--network none`, capped, no host mounts, destroyed after use |

## Thin by design

A service wires packages together and owns transport. It does not own logic. If you are
writing a rule, a policy, a score or a decision inside `services/`, it belongs in a package
and the service should be calling it.

This is what makes the eventual split into separate deployables a deployment change rather
than a rewrite — the handoff's ADR-007 judgement, which still holds.

## Identity

`api` derives identity from a **verified session token**, never from a request body, and
the discard of any caller-supplied identity field is **visible in the code** rather than
implied. Keep that habit from the prototype: an explicit discard is the thing a reviewer
can point at.

## The GPU box runs nothing from here

It runs the inference runtime and nothing else. That is what makes its firewall rule
readable in five seconds (see `packages/sovereignty/AGENTS.md`).
