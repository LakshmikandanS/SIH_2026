# AGENTS.md — Citadel (Sovereign Agentic AI Workbench)

This file is the entry point for any coding agent (Claude Code, Cursor, Codex CLI, Windsurf,
Aider, or a human) working in this repository. Read this fully before writing code.

---

## Read these two first

**`docs/CITADEL_TARGET_ARCHITECTURE.md` is the destination.** It describes what Citadel is being
built toward, derived from the system's purpose and threat model rather than from what the code
currently contains. When the code and that document disagree, **the document is right and the code
is what changes.** Do not narrow a design to fit the current package layout.

**`docs/CITADEL_P1_AUTHORIZATION_SPINE.md` is the work in progress.** P1 makes the target's core
invariant true: every privileged operation is decided by the Control Plane, proven by a signed
receipt the executing service verifies for itself, and recorded in a chain nobody can rewrite. It
has an eight-step work order; the three demo paths must pass at the end of every step.

The subagent that owns this phase is `.claude/agents/authorization-spine.md`. P1 is the only phase
with a frozen contract — P2 through P5 are named in the target architecture and nobody should be
building them yet.

---

## 0. Source of truth

**`docs/CITADEL_MVP_DESIGN.md` is the frozen contract for the vertical slice that exists today.**
Every schema, state machine, endpoint contract, and pseudocode block in it was meant to be typed in
directly, not redesigned, and most of it still stands unchanged.

Precedence, in order:

1. `CITADEL_TARGET_ARCHITECTURE.md` — the destination. Wins on any question of what the system
   should become.
2. `CITADEL_P1_AUTHORIZATION_SPINE.md` — supersedes the MVP contract for the authorization path
   (§6.5, §6.6, §6.7's call path) and nothing else.
3. `CITADEL_MVP_DESIGN.md` — everything else, still binding.

If this AGENTS.md or a subagent file conflicts with any of the three, the document wins — fix the
AGENTS.md, not the code.

`docs/CITADEL_BLACKBOX_AUDIT.md` and `docs/architecture/00`–`14` are background: they describe the
full production-scale architecture and the gaps found in it. The MVP design doc resolved every one
of those gaps for the slice (its §7 Closure Matrix) and retires some older shapes outright — do not
resurrect a superseded shape because it appears in the background docs.

## 1. What we are building

One scenario, end to end, still the working demo and still the definition of done:

> A maintenance engineer asks Citadel to identify Pump P-101's recent maintenance history from
> internal (CONFIDENTIAL) documents and generate a short summary report, which a human approver
> then releases.

Two more paths are **mandatory**, not optional polish:
- A **denial path**: the agent attempts an out-of-scope retrieval (wrong department ACL) and is
  refused before any data leaves the Data Plane.
- An **emergency-control path**: an admin runs `disable-tool python.execute` and a subsequent
  attempt is denied even with an otherwise-valid capability token.

**All three must pass at the end of every phase and every step within a phase.** A refactor does not
get to suspend them.

## 2. The one architectural rule that governs everything

> Agents do not get direct authority. They request capabilities; the control plane decides
> whether those capabilities may be used.

The target architecture extends this one step further, and P1 implements the extension: **the
orchestrator is not a security boundary either.** It holds model output, so it inherits the model's
untrustworthiness. No component acts on a privileged operation because it trusts its caller — it
verifies a signature or it refuses.

## 3. Trust model

Today the system runs as two processes: the trusted workflow zone (`app.main`) and the isolated
execution zone (`execution_service`, the only Docker-socket holder). The MVP design doc §2 states the
limitation this leaves — the Control Plane shares a process with the Orchestrator, so a compromised
Orchestrator could bypass the in-process policy call.

**P1 dissolves that limitation rather than relocating it.** Once every executing service verifies a
signed Decision Receipt before acting, where a component runs stops being a security property. The
process split into five services follows in P2 as a deployment change, not a security change. See
`CITADEL_TARGET_ARCHITECTURE.md` §2 and §6 for why that ordering is deliberate.

## 4. Repository layout

The current layout is a single `app/` package plus a separate `execution_service/`. The target
(`CITADEL_TARGET_ARCHITECTURE.md` §4) is `contracts/` + `services/*` + `clients/*`, where **no
service imports another service** and shared meaning lives only in `contracts/`.

P1 creates `contracts/`. P2 dissolves `app/` into `services/`. Until then both shapes coexist:

```
citadel/
├── contracts/                    ← P1: the shared language, imports nothing else
├── app/                          ← dissolved into services/ in P2
│   ├── db/ observability/ identity/ policy/ capability/
│   ├── tool_gateway/ model_router/ orchestrator/ rag/ artifact/ approval/ ui/
├── execution_service/            ← already a separate process; becomes services/execution/
├── cli/                          ← becomes clients/cli/
├── data/                         ← demo corpus + mandatory .meta.json ACL sidecars
├── docs/
└── tests/
```

Do not add new top-level packages that are not in the target shape, and do not deepen `app/` —
anything new belongs in `contracts/` or in the service it will live in after P2.

## 5. Stack

FastAPI · SQLAlchemy over SQLite (Postgres by changing one URL) · a local vector store · Docker SDK
for Python inside the Execution Service only · Ollama for local reasoning and embeddings ·
Ed25519 for all token signing from P1 onward.

## 6. Build history and subagents

The MVP was built in this order, each step demoable before the next began, each owned by a subagent
under `.claude/agents/`:

| Step | Subagent | Deliverable |
|---|---|---|
| 3 | `foundation-schema` | Core tables + the single Observability writer |
| 4 | `security-control-plane` | Identity → Policy → Capability → Tool Gateway, proven with a fake echo tool |
| 5 | `execution-service` | Execution Service + one-shot `python.execute` container |
| 6 | `data-plane-rag` | Ingestion with mandatory ACL sidecar → embed → filtered retrieval |
| 7 | `orchestrator` | Task intake → plan → agent loop → real tool calls → finish/revise |
| 8 | `artifact-pipeline` | generate → verify → approve → release |
| 9 | `cli` | `login`, `/task`, `/status`, `/approve`, `/trace`, `admin disable-tool` |
| — | `qa-tester` | Runs after every step; owns `tests/` |
| **P1** | `authorization-spine` | `contracts/` + signed Decision Receipts verified by every executing service |

## 7. Conventions for every subagent

- **Design from the target, not from the current layout.** If the right shape requires moving or
  deleting existing code, that is the expected cost, not a reason to compromise. The one thing that
  does not bend is §1's three demo paths.
- Never invent a schema, endpoint shape, or state name that is not in the governing document. If
  something genuinely is not specified, say so and propose the smallest addition rather than
  guessing silently.
- Every mutating endpoint derives identity from the verified session token, never from the request
  body — no exception, including the approval endpoint.
- Every privileged operation carries a decision receipt from P1 onward, and the executing service
  verifies it itself, immediately before acting.
- Every event goes through the single Observability writer. No other module computes or appends to
  the hash chain.
- Fail closed by default: an unmatched policy check is `DENY`, an unverifiable receipt is a refusal,
  an unknown classification is not comparable and therefore not permitted, an unmatched verification
  check fails the artifact. No silent passes.
- Every claimed boundary has a structural test that fails when it is crossed, and every such test has
  a negative control proving it can fail.
