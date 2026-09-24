# ADR-0008 — A workbench where agents and people write reports together

**Status:** Accepted · **Date:** 2026-09-23
**Amends:** ADR-0001 §Q5 (one agent loop per task) and `packages/runtime/AGENTS.md`
(single-agent loop); `web/AGENTS.md` (a task page per view). Adds the `report` template
beside the three constructed in ADR-0001 §Q11.

---

## Context

The first build ran one agent per task and showed its journal on a page of its own. Three
requests from Fahim changed the shape of the product:

1. The vision image: an IDE-style **workbench** — a resource tree on the left, editor tabs
   and a command line in the centre (`/task report on lathe`, `/ask the difference in
   policy 1 between today and last month`), and observability, sandbox state and an
   activity feed on the right. Agent cards say what each agent is doing and waiting for.
2. "The citadel should resemble the workbench where **human too can work** if needed."
3. "For now, the main task to focus is **report writing**: agent can give report
   accordingly to user prompt, user can modify if he wants."

The third is the priority. The first two describe the room it happens in.

## Decision

### A report is shaped by the request, then owned by the person who asked

- **A new template, `report`** (`registry/templates.yaml`). Its body is a `sections` field:
  a list of `{heading, text}` that the writer fills with **one section per thing the
  person asked for, in the order they asked, headed in their words**. Every paragraph
  keeps its own citations, and verification checks each one on its own: tier 3 per
  section, tier 4 (grounding) per paragraph. Summary, recommendations and open questions
  sit around the body. The last two are `omit_when_empty`, so a report on "only its
  condition and the vendor options" is those two sections, not two more headings reading
  "Not applicable."
- **No approval block.** The author owns a report. Release through an approver is still
  there for the approval note, the inspection summary and the calculation sheet.
- **The person edits it.** The report tab is a structured editor over the template's own
  sections: headings, paragraphs, add, move and remove a section, and click an evidence
  item to insert its id. **Save** renders and verifies the edit as a **new version**
  (`POST /api/artifacts/{id}/edit` → `citadel_runtime.after_edit`). An edit that fails a
  tier is kept as a failed draft and the last verified version stands. Numbers that
  cannot be traced to cited evidence are flagged, never hidden.
- **Or sends it back.** **Revise** (`POST /api/tasks/{id}/revise`, at most five times)
  returns the task to its agents with an instruction. The lead starts from the **newest
  verified version, hand edits included**. It gets that version in a standing `brief`
  ("REVISION REQUESTED: … CURRENT VERSION (v2 …, Edited by …) -- revise THIS"), which is
  persisted so that a worker that dies mid-revision resumes with it.
- **Every version is kept.** Superseded versions are history. Only the newest may be
  edited or decided on (`LATEST_VERSION`), and each version's revision history names who
  made it.

### More than one agent, one shared state

- The planner may add up to three **helper agents** (`prompts.MAX_AGENTS`). Each has a
  one-sentence goal, the tools it should favour, and the agents whose findings it needs
  (`depends_on`). A `TaskRun` coordinator runs them in dependency waves on a thread pool,
  each in its own `AgentLoop` with its own step budget (`BudgetLimits.agent_steps`). The
  lead then writes the deliverable from their findings.
- **Shared state** (`task_shared_state`): the plan, decisions, discovered facts,
  assumptions, questions for a person, and notes. Agents write to it with the
  `state.note` tool. It is shown to every agent at its next step, and it survives
  resumption.
- `/ask` is a task of kind `ask`: a short answer with citations and no deliverable. It can
  also answer questions about the work itself through `workbench.inspect` ("what is agent
  2 doing?").

### People work in the same room, through the same doors

- A person may **pause, resume or cancel** their task, **add** to its shared state, or
  **steer** one agent (`/steer agent_2 …`, delivered at that agent's next step).
- A person may **run a tool** on their own task (`/run docs.search {...}`). It goes through
  the policy chokepoint with a signed receipt, exactly as an agent's call does, under the
  identity `human:<id>`.
- **Task files** (`/task <name>`, `task_drafts`) let a person write a long request as a
  file and **Commit** it. The file stays as the record of what was asked.
- Every human act is journalled with who did it and audited
  (`task.human_step`, `task.note_added`, `task.draft_committed`, `task.revision_requested`,
  `artifact.edited`). The journal still reconstructs the task.

### The workbench

`web/src/workbench.js` and `workbench-tabs.js` lay the vision out. Plain JavaScript, same
origin, no external host (invariant 10).

| Region | What it holds |
|---|---|
| Left | **RESOURCES**: the folder tree of the documents the person may read (every version kept), and the database. **REPORTS**: the person's tasks. **TOOLS RUNNING**, with a Tools panel listing what is available, what is active and what was used |
| Centre | Tabs for the welcome page, task files, tasks, `agent_N.plan`, reports, documents, panels and memory. Below them, a **command line**: `/report`, `/task`, `/revise`, `/ask`, `/note`, `/steer`, `/pause`, `/run` and more |
| Right | **Observability**: model evaluation, agent behaviour, DB monitoring, alerts, security, policy auditing, trace, container health and resource metrics. **Environment / sandbox state**. **Activity**: a feed written in words |

The APIs behind these panels read the journal, the audit log, Postgres statistics and
`service_heartbeats`. Heartbeats come from the API and worker, which report process
statistics from `/proc` using the standard library only.

## What this gives up

- **A report without an approver is only as final as its author makes it.** That is the
  point of the request. The approval path remains for the templates that declare one.
- **More agents cost more model calls** on one GPU. Helpers are bounded, three at most
  with six steps each, and they queue behind GPU admission like any other call. The
  planner adds them only when the request has independent parts.
- **A person's edit is verified like an agent's.** A section a person adds must cite, as
  any body section must. Their uncited numbers are flagged. This is deliberate: the
  grounding promise does not depend on who typed the sentence.

## Revisit when

- A real organisational report template arrives. It replaces `registry/templates/report.docx`
  with no code change, provided its optional headings sit directly before their
  placeholders.
- A second GPU exists. Helper agents could then run truly in parallel rather than
  queueing for admission.
