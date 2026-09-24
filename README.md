# Citadel

A sovereign, air-gapped agentic AI workbench for confidential industrial work.

Refineries, PSUs, defence-linked manufacturers and government offices produce a lot of
routine but sensitive knowledge work: approval notes, engineering calculations, internal
tooling, reviews of scanned drawings and inspection reports. None of it can go to a cloud
assistant. Citadel runs entirely on the organisation's own hardware. It produces real
deliverables rather than chat replies, and it proves that nothing left the building.

You describe the work in your own words and an agent (or a small team of them) does it:

- **Plans** the task from your goal.
- **Uses only what you are cleared for**: the tools and documents your identity allows.
- **Cites every fact** back to a page and a region of the source.
- **Writes the report you asked for**, with one section per thing you asked for, in a
  workbench where you can edit any section yourself or send it back for revision.
- **Runs code in a sandbox.**
- **Hands deliverables to an approver** where a template asks for one: a Word approval
  note, an Excel calculation, or a verified script.

Every model call is routed with a visible reason. Every decision lands in a hash-chained
audit log. The sovereignty panel shows, live, that no connection left the deployment.

## Run it on Windows: one command

**New machine?** Run `setup` in the repository folder first. It checks Windows, the GPU,
Docker Desktop, Ollama, the port and the models. It offers to install or download what is
missing, then starts Citadel and verifies it. [SETUP.md](./SETUP.md) has the same steps by
hand, the WSL2 configuration (`setup wsl2`), and troubleshooting.

You need:

- **Docker Desktop**, running.
- **Ollama for Windows** (ollama.com). It serves the models and keeps the GPU.
- **About 12 GB of free disk**: roughly 7 GB for the four models, the rest for the
  container image and the database.
- **An NVIDIA GPU with a current driver.** The demonstration is sized for an 8 GB RTX
  5060. Without a GPU, Ollama falls back to the CPU, slowly.

From the repository folder, in Command Prompt (in PowerShell, type `.\citadel`):

```
citadel            build if needed, start everything, open the browser
citadel models     one time: download the four approved models (about 7 GB)
```

**The first `citadel` builds the image.** That takes a few minutes, because it downloads
the Python packages and the Postgres image; later starts take seconds. The launcher then:

1. Waits for the API.
2. Tells you whether the models are in.
3. Opens `http://127.0.0.1:8000`.

**Once the models are installed, the worker ingests the demonstration corpus by itself.**
That is sixteen documents in department folders, three of them scans, plus a later issue
of Policy 1, and takes a few minutes. The worker waits
for the models on purpose: scans ingested without them would be read without vision and
indexed without embeddings, and would stay that way.

| Command | What it does |
|---|---|
| `citadel` | Build if needed, start, open the browser |
| `citadel models` | `ollama pull` every model `registry/models.demo-local.yaml` approves |
| `citadel status` | Containers, API health, missing models |
| `citadel logs` | Follow the logs of every service |
| `citadel stop` | Stop. Documents, results, keys and the audit log stay (Docker volumes) |
| `citadel reset` | Stop and delete all of it (you have to type DELETE) |

**Optional settings**, set before running:

- `CITADEL_PORT`: the port to serve on (default 8000).
- `CITADEL_REQUIRE_ENFORCEMENT=1`: refuse to start if the egress ruleset cannot be applied.
- `CITADEL_DB_PASSWORD`: the database password.

## The demonstration

Sign in as one of three seeded identities. There is no password at demonstration time
(ADR-0001 §Q7).

| Identity | Role | Clearance | Department |
|---|---|---|---|
| R. Kulkarni (`demo-engineer-1`) | engineer | INTERNAL | process-engineering |
| S. Nair (`demo-engineer-2`) | engineer | CONFIDENTIAL | instrumentation |
| V. Rangan (`demo-approver`) | approver | CONFIDENTIAL | quality-assurance |

### Write a report: the workbench

The **Workbench** (`#/work`) is where reports are written: a resource tree on the left,
tabs and a command line in the middle, and observability, sandbox state and activity on
the right ([ADR-0008](./docs/adr/0008-a-workbench-where-agents-and-people-write-reports-together.md)).

1. **Ask for the report you want.** As R. Kulkarni, type in the command line:
   `/report on lathe L-1 covering only its condition and the vendor options`. You can also
   use the welcome tab, or `/task lathe report` to write a longer request as a file and
   **Commit** it.
2. **Watch it work.** The task tab shows the agent cards (click one: goal, current step,
   completed work, tools, what it waits for), the live journal and the shared state.
   A fuller request such as "…its condition and workload, what company policy requires
   today, the options, and a recommendation" is split among helper agents.
   - `/pause` pauses the task and `/steer agent_1 …` tells one agent something at its
     next step.
   - `/note` adds to the shared state.
   - `/run docs.search {"query": "L-1 runout"}` runs a tool yourself, through the same
     policy chokepoint.
3. **Open the report.** It has exactly the sections you asked for, each paragraph cited.
   *Web search* facts come from the offline reference library
   ([ADR-0010](./docs/adr/0010-web-search-is-the-offline-reference-library.md)).
4. **Change it yourself.**
   - You can edit the title, the summary or any section, add or reorder sections, and
     click an evidence item to cite it.
   - **Save as v2** renders and verifies your version. A number you add without a source
     is flagged, not hidden.
5. **Or ask for changes.** In **Revise**, type "Add a section on operator training". The
   agents start from your latest verified version, so your edits stay, and write v3. Every
   version is kept, can be downloaded as .docx, and names its author in the revision
   history.
6. **Ask about the documents.** `/ask the difference in policy 1 between today and last
   month` compares the two issues of Policy 1 and cites both. `/ask what is agent 2
   doing?` answers about the work itself.

What the workbench learns from finished work (outcomes, facts and approvers' comments)
is kept by the memory manager
([ADR-0009](./docs/adr/0009-the-memory-manager-monarchs-design-in-citadels-store.md)).
Agents recall it before they plan, and you can read, edit and archive it in the Memory
tab. It is filtered by department and clearance, like documents.

### The five acceptance targets

The **Task log** (`#/tasks`) has a one-click example goal for each target. Nothing is
scripted behind them: the planner sees whatever goal text you submit.

### B: scanned report → approval note (.docx)

1. As R. Kulkarni, choose *Approval note (scan → .docx)* and submit.
2. Follow the journal. It shows the plan, each tool call as it passes through the policy
   chokepoint, and the evidence gathered: E1, E2… are document regions, C1… are
   computations.
3. The note is drafted from the approval-note template and verified in four tiers:
   structure, schema, citations, and grounding (every number must trace to a cited block).
   It then goes for approval.
4. Click a citation chip. The scanned page opens with the exact region highlighted.
   You can also preview the .docx in the browser.
5. Sign in as V. Rangan, open *Approvals*, and approve. Or reject it with a comment, and
   the task revises once.

Approval re-renders the note with the approval block filled in, hashes it, and releases it
with its provenance record. The person who asked for a deliverable cannot approve it.

### C: code written and run in a sandbox

*Sandboxed code* asks for a remaining-life calculation from the inspection readings.

1. The agent writes Python.
2. The chokepoint issues a signed, single-use receipt bound to that exact source.
3. The sandbox verifies the receipt before running anything. The sandbox is its own
   container, on a network with no route out.
4. The output becomes evidence C1, and the script is kept as a verified artifact.

### D: vision

Scanned pages are read twice at ingest: once by OCR, and once by a vision model that reads
stamps, signatures and filled-in fields. *Vision: read the stamp* asks who signed
inspection report IR-2026-0147. The agent re-reads that region of the scan with the vision
model and cites it.

### A: model routing, with the reason visible

Every model call in a task's journal shows which model was chosen, with each candidate's
score: capability, task fit, quality, and residency (a loaded model beats one that has to
be swapped in). *Models & routing* runs the same router live for planning, reasoning,
code, vision and embedding requests. Code goes to the coder model, planning to the general
model, and scans to the vision model.

### E: zero egress, shown live

*Sovereignty* shows:

- **External connections observed**: the number that must read zero.
- **Every outbound attempt** the monitor recorded.
- **The ruleset applied** in each container.
- **A deliberate probe**: a button that makes the API and the sandbox try to reach the
  internet. The attempts are refused and recorded, and a running task carries on.

Each task also has its own sovereignty report.

The strongest version of the test comes from ADR-0004: unplug the network cable or turn
off Wi-Fi, then run the whole flow again. Nothing degrades, because nothing was reaching
out. What the container rules cover, and what they do not (the host, and Ollama on it), is
written down in [ADR-0006](./docs/adr/0006-docker-desktop-launcher-and-per-container-enforcement.md),
and the panel says it too.

### Access control and audit

**Access control.** *Search & access* runs one query as all three identities side by side.
The same question returns different citations for each identity, with the denials counted.
`ops/demo/corpus/manifest.yaml` shows who may see what.

**Audit.** *Audit & policy* shows the hash-chained audit log: every policy decision (allow
and deny), receipt, model pull, approval and release. It can verify the chain end to end.

## For developers

```bash
scripts/check.sh                   # pytest + mypy --strict + ruff: "is the repo green"
scripts/run.sh --fake-models       # the whole system without containers, with a scripted model stand-in
scripts/run.sh                     # the same, against a real Ollama at 127.0.0.1:11434
scripts/up.sh [up|models|status|logs|down]   # the Compose stack on Linux or WSL2 (ADR-0005)
```

- **Postgres-backed tests** need `scripts/dev-db.sh start` first; it prints the `PG*`
  variables to export. Without it those tests skip instead of failing. `scripts/run.sh`
  starts the database for you.
- **The shell scripts need bash** (Linux or WSL2). On plain Windows, `uv run pytest`,
  `uv run mypy --strict …` and `uv run ruff check .` work directly.
- **After pulling changes that touch a `pyproject.toml`**, run `uv sync`; it also updates
  `uv.lock`.

[`AGENTS.md`](./AGENTS.md) is the rulebook: the invariants, the module map and the current
state. Read it before changing anything. Decisions are in [`docs/adr/`](./docs/adr/).

## Shape

A modular monolith plus isolated executors. The demonstration runs on **one box**
(ADR-0004): the RTX 5060 workstation runs Ollama, Postgres, the API, the worker, the
sandbox and everything else. Module boundaries are enforced by structural tests, so
splitting into separate services or machines later is a deployment change, not a rewrite.

```
packages/    contracts platform gateway knowledge memory tools runtime deliverables sovereignty
services/    api (HTTP + SSE + the web UI) · worker (agent loop, ingestion) · sandbox (code execution)
registry/    models, tools, policy, roles, events, templates — data, never code
web/         the workbench UI (no build step, no external URL)
tests/       structural (boundary enforcement) · fakes (a scripted Ollama stand-in)
ops/         compose (the one-box stack) · nftables · demo corpus · templates · wsl2 · hpc
citadel.cmd  the Windows launcher
```

## Profiles

| | `demo-local` | `hpc-eval` |
|---|---|---|
| Role | **The demonstration.** Sovereign. | **A measuring instrument.** Never sovereign. |
| Hardware | RTX 5060, 8 GB, single box | University HPC, DGX-H200 via SLURM |
| Runtime | Ollama | vLLM |
| Corpus | Real | Synthetic only, enforced at ingest |

**Design to `demo-local`, and demonstrate on it too.** The problem statement asks for a
single workstation with a mid-range GPU, so that is the product. `hpc-eval` benchmarks
models and runs the eval harness. It is never on the demonstration path, and acceptance
target E is never shown there (ADR-0003).

## Not done yet

- **Two sandbox-driven substitutions still stand**: Starlette + uvicorn instead of
  FastAPI, and hand-written HTML/CSS/JS instead of Vite + React (`services/AGENTS.md`,
  `web/AGENTS.md`).
- **Deliverables** are .docx and .xlsx. PowerPoint is not built.
- **Report writing has been walked end to end with the scripted stand-in model**, in a
  browser. How well the local models follow the section instructions is to be measured
  on the real card, alongside structured-output conformance.
- **The M1 measurements on the real card have not been run**: resident-set VRAM, swap
  cost, and structured-output conformance per model. The registry's figures are estimates
  and say so.
- **Not built**: the offline install drill (PLAN-M0 task 14), the two-box Compose
  override, and the host-level ruleset for a WSL2 distro (ADR-0006).
- **The Compose stack has not yet run on a real Docker daemon.** It was built where no
  Docker daemon could run. There, the process model was exercised in network namespaces:
  the users, capabilities, egress ruleset, environment and service order, with every
  flow walked end to end. The first real `docker compose up` happens on your machine. If
  the egress ruleset cannot be applied there, the sovereignty panel says so in words.
- **Demonstration sign-in has no password.**

## Related repositories

- `AI_WORKBENCH/MONARCH` is a local assistant's memory. Citadel implements its memory
  manager's design in its own Postgres store, with classification and ACL on every memory
  (ADR-0009). There is no code dependency and no merge.
- `AI_WORKBENCH/CITADEL` is the prototype: a quarry, not a baseline. `contracts/` was
  worth porting; most of the rest is documented in `AGENTS.md` as failure modes with names.
