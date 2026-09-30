# Citadel

**A sovereign, on-premise agentic AI workbench for confidential industrial work.**

Refineries, PSUs, defence-linked manufacturers and government offices generate a lot of routine
but sensitive knowledge work — approval notes, engineering calculations, inspection reports,
P&IDs. None of it can go to a cloud assistant. So the work is either done by hand, or someone
quietly pastes confidential material into a public tool anyway.

Citadel runs the whole loop on the organisation's own hardware, and — the part that actually
matters — it does not ask you to trust that claim. Every authorization decision, every denial,
and every release is recorded in a hash-chained audit log you can replay, and the sandbox's lack
of network access is asserted by a test that tries to make an outbound call and requires it to
fail.

> **Status:** the MVP vertical slice is complete and runs end to end. 134 tests pass.
> `docs/CITADEL_MVP_DESIGN.md` is the frozen contract; `docs/BUILD_LOG.md` records what each
> build step actually produced and the decisions later steps must respect.

---

## What it does, concretely

A human submits a task in plain language. A local model plans it. A deterministic agent loop
executes that plan, and **every tool call the agent attempts passes two independent checks**
before anything runs. Retrieval is filtered by ACL *inside* the data plane. Code the model wrote
runs only in a throwaway container with no network. The result is a real document, structurally
verified, that stays unreleased until a named human approves it.

Three things are demonstrable on demand, and they are the point:

| | What it proves |
|---|---|
| **Happy path** | The whole loop works: plan → retrieve → compute → generate → verify → approve → release |
| **Denial path** | An out-of-scope retrieval is refused. The capability is *valid*; policy says no. The tool never runs |
| **Kill-switch** | An admin disables a tool; an agent holding a still-valid, unexpired token is denied on its next call |

---

## Quickstart

**Prerequisites**

- Python 3.12
- [Docker Desktop](https://docs.docker.com/desktop/) running (`docker version` must return a *server* version)
- [Ollama](https://ollama.com/) with two models pulled:
  ```bash
  ollama pull hermes3
  ollama pull nomic-embed-text
  ```

**Run it**

```bash
.\run.ps1
```

That is the whole thing. The script creates the virtualenv if missing, installs dependencies,
checks Docker and Ollama (naming exactly what to do if either is not ready), frees the ports if a
previous run is still holding them, starts both processes, waits for each to report healthy, and
opens the console.

On Linux/macOS use `./run.sh` — same behaviour.

| | |
|---|---|
| `.\run.ps1 -Check` | verify prerequisites and exit |
| `.\run.ps1 -Stop` | stop both processes |
| `.\run.ps1 -StableSecrets` | keep sessions alive across restarts (see below) |
| `.\run.ps1 -NoBrowser` | don't open a browser |

Logs land in `var/logs/`. Then open **<http://127.0.0.1:8420/ui>**.

<details>
<summary>Starting it by hand instead</summary>

Two processes, two terminals. The Execution Service is a *separate OS process* on purpose — it is
the only thing in the system that holds a Docker socket, and that separation is the one real
security boundary in this slice. Start it first; `python.execute` fails confusingly if nothing is
listening for it.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

.venv\Scripts\python.exe -m execution_service   # terminal 1
.venv\Scripts\python.exe -m app.main            # terminal 2
```

The per-image lists in `docker/` are deliberately narrower — `app.requirements.txt` must never
gain the `docker` package, because the trusted zone must have no code path to a Docker socket.
`requirements-dev.txt` is the developer machine, where both zones and the tests share one venv.
</details>

> **Signing secrets are per-process by default**, so restarting the server ends your session and
> the console will ask you to sign in again. That is the correct fail-closed behaviour and no dev
> secret is committed. For an uninterrupted demo, `.\run.ps1 -StableSecrets` generates them once
> into `var/secrets.env` (gitignored) and reuses them. Development convenience only.

Demo users are seeded automatically on first start:

| User | Password | Role | Department |
|---|---|---|---|
| `j.rao` | `engineer-pw` | engineer — submits tasks | maintenance |
| `a.singh` | `approver-pw` | approver — releases artifacts | maintenance |
| `s.mehta` | `admin-pw` | admin — kill-switch | security |

Roles are genuinely enforced. The engineer who submits work **cannot release it**, and the admin
**cannot read any document** — they hold the kill-switch, not a master key.

---

## Two ways to drive it

### The browser workbench — `/ui`

A three-column console: agent state, corpus and tools on the left; task prompt, generated report,
audit trace and file viewer in the centre with an embedded CLI; observability, sandbox state and
live activity on the right.

Click a file in *Resources / DB / Files* to open it — access uses the same ACL rule `rag.search`
uses, so a document you could not retrieve through an agent is equally unreadable to you here.

### The shell client

```bash
.venv\Scripts\python.exe -m cli login                       # prompts; or -u <user> -p <pass>
.venv\Scripts\python.exe -m cli task "Identify Pump P-101's recent maintenance history and generate a short summary report." --classification CONFIDENTIAL
.venv\Scripts\python.exe -m cli status  <task_id>
.venv\Scripts\python.exe -m cli trace   <task_id>
.venv\Scripts\python.exe -m cli approve <task_id>           # must be signed in as the approver
.venv\Scripts\python.exe -m cli reject  <task_id> --comment "..."
.venv\Scripts\python.exe -m cli admin disable-tool python.execute
```

`login` takes `-u`/`-p` for scripting; without them it prompts. The session token is cached in
`~/.citadel/session.json` — override the location with `CITADEL_CLI_HOME`, which is handy for
running several personas side by side.

**`admin disable-tool` is one-way.** §6.8 names exactly one emergency control, so there is no
`enable-tool` and no re-enable endpoint. The flag lives in the server process; restart it to
clear. That is deliberate — an emergency stop you can quietly undo is not much of an emergency
stop.

---

## Try the three demo paths

**1. Happy path.** Sign in as `j.rao`, submit the default task, then sign in as `a.singh` and
approve. Open the `report.md` tab — you get a real document with computed values and a source
list. Check `trace`: an unbroken hash chain from `TASK_CREATED` to `ARTIFACT_RELEASED`.

**2. Denial path.** Click *run denial-path task*. The agent asks for the Q3 finance report. Its
capability is valid, but the finance document's ACL is disjoint from the task's department, so it
is filtered inside the data plane and never reaches the agent — visible as `filtered_documents`
on the `EVIDENCE_RETRIEVED` event.

**3. Kill-switch.** As `s.mehta`, disable `python.execute`. Run a task as `j.rao`. The capability
token is still valid and unexpired, and the call is still denied. The task ends:

```
status : FAILED
reason : step S2 (python.execute) failed after 2 attempt(s):
         TOOL_DISABLED tool 'python.execute' disabled by administrator
```

That is the whole reason capability and policy are kept as two separate checks — nothing had to
be revoked and no token had to be hunted down; the policy layer simply answers differently.
Restart the server to clear the flag.

---

## Architecture in one screen

```
  Engineer / Approver / Admin
            │
        CLI  or  /ui
            │
┌───────────▼──────────────────────────────────────────┐
│ TRUSTED WORKFLOW ZONE — one FastAPI process          │
│                                                      │
│  Query Router → Orchestrator (PLAN→ACT→OBSERVE)      │
│                      │                               │
│                      ▼                               │
│               TOOL GATEWAY  ← the only way to a tool │
│                 A. verify capability (local)         │
│                 B. policy decision (this resource)   │
│                 C. route to backend                  │
│                      │                               │
│    ┌─────────────────┼──────────────────┐            │
│    ▼                 ▼                  ▼            │
│  rag.search    python.execute    generate_report     │
│  ACL-filtered        │           → Verifier          │
│  in the data plane   │           → human approval    │
└──────────────────────┼───────────────────────────────┘
                       │ HTTP  (no Docker socket here)
┌──────────────────────▼───────────────────────────────┐
│ ISOLATED EXECUTION ZONE — separate OS process        │
│   Execution Service — the only Docker-socket holder  │
│     └─ one-shot container: no socket, no host mount, │
│        NO NETWORK, cpu/memory/pids caps, destroyed   │
└──────────────────────────────────────────────────────┘
```

Everything is recorded by a **single serializing writer** into a SHA-256 hash chain. `append_event`
is the only code path that writes the event table, and a test greps the source tree to keep it
that way.

For the full walkthrough of what happens on a single task — every function, in order — see
**[docs/WORKFLOW.md](docs/WORKFLOW.md)**.

---

## The security model, briefly

**Capability ≠ policy, deliberately.** A capability token proves *"this agent may attempt
`rag.search` in general"* — it is signed HMAC-SHA256, scoped to one operation and one task/agent
pair, and lives 5 minutes. Policy decides *"is THIS specific document allowed, right now"*, and
runs on every single call. Most agent frameworks collapse these into one check. Keeping them
apart is what makes the kill-switch work against tokens that are already issued and still valid.

**The policy engine is four ordered rules, first match wins, default DENY.** Not a rules engine,
not a DSL — that generality is explicitly deferred. Read it in `app/policy/engine.py`; it is
short on purpose.

**Filtering happens inside the data plane.** A document you may not see is never built into a
result and then dropped — it never enters the candidate list at all.

**Identity always comes from the verified session JWT.** A `user_id` or `approver_id` in a request
body is ignored, on every endpoint, without exception.

---

## Testing

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
```

**134 passed** with Docker and Ollama available. Tests that need a live daemon skip cleanly with a
clear reason when it is absent — so *check for skips before believing a green run*.

Per-step demo scripts print what they prove rather than asserting it silently:

```bash
.venv\Scripts\python.exe -m tests.demos.step4_security      # ALLOW + all four DENY paths
.venv\Scripts\python.exe -m tests.demos.step5_execution     # sandbox, and the no-network proof
.venv\Scripts\python.exe -m tests.demos.step6_rag           # ACL-filtered retrieval
.venv\Scripts\python.exe -m tests.demos.step7_orchestrator  # a real end-to-end task
.venv\Scripts\python.exe -m tests.demos.step8_artifact      # generate → verify → approve
```

---

## Configuration

Everything is environment-driven. Nothing needs setting for a local run.

| Variable | Default | Notes |
|---|---|---|
| `CITADEL_DATABASE_URL` | `sqlite:///var/citadel.db` | Point at PostgreSQL and nothing else changes |
| `CITADEL_SERVER_PORT` | `8420` | Trusted-zone API and `/ui` |
| `CITADEL_EXECUTION_SERVICE_URL` | `http://127.0.0.1:8901` | Where the trusted zone reaches the sandbox |
| `CITADEL_SESSION_SECRET` | *random per process* | **Unset means a restart invalidates every session.** Set it for anything long-lived |
| `CITADEL_CAPABILITY_SECRET` | *random per process* | Deliberately separate from the session secret |
| `CITADEL_OLLAMA_URL` | `http://localhost:11434` | Local inference only |

No development secret is committed. If the signing variables are unset, a random per-process key
is generated — so a restart invalidates outstanding tokens. That is the fail-closed failure mode;
a checked-in "dev secret" is the one that reaches production by accident.

---

## What this slice deliberately does not build

Kubernetes, Kafka, Redis, a policy DSL, a multi-agent framework, Docker-socket access for agents,
direct agent writes to shared state, internet access for the execution zone, an LLM-as-judge
verifier, or a production dashboard. Each maps to a documented `DEFERRED` or `NOT_APPLICABLE` row
in the design doc's closure matrix — none is an accidental omission.

Known limitations, stated plainly rather than buried:

- The control plane shares a process with the orchestrator. A compromised orchestrator could in
  principle bypass the in-process policy call. The boundary that *is* real is the one around code
  execution, because that is the only place adversarial content runs.
- Artifact immutability is enforced at the API layer, not by write-once storage.
- One agent per task — `Agent.task_id` is UNIQUE by schema. Multi-agent is a deliberate Phase-2
  decision, not an oversight.
- The local user table stands in for LDAP/AD, and there is no capability revocation list; the
  5-minute TTL plus the independently-enforced kill-switch cover it for this slice.

---

## Repository layout

```
app/
  identity/      login, bcrypt, session JWT
  capability/    scoped 5-minute capability tokens
  policy/        the four-rule engine + the one kill-switch
  tool_gateway/  the single authorization chokepoint
  rag/           ingestion with mandatory ACL sidecars, filtered retrieval
  execution/     HTTP client to the sandbox (imports no docker)
  orchestrator/  task intake, planning, the agent loop
  model_router/  static model selection + the Ollama client
  artifact/      template, Verifier, artifact read
  approval/      the one transactional release endpoint
  observability/ the single serializing hash-chain writer
  ui/            the browser workbench
execution_service/  the isolated zone — the only module importing docker
cli/             the shell client
data/            demo corpus, each file with a mandatory .meta.json sidecar
docs/            the frozen design contract, build log, and workflow guide
```

---

## Documentation

- **[docs/WORKFLOW.md](docs/WORKFLOW.md)** — what happens on one task, step by step through the code
- **[docs/CITADEL_MVP_DESIGN.md](docs/CITADEL_MVP_DESIGN.md)** — the frozen contract every component implements
- **[docs/BUILD_LOG.md](docs/BUILD_LOG.md)** — what each build step produced, and the decisions later steps must respect
- **[AGENTS.md](AGENTS.md)** — build order and the subagent that owns each slice
