# AGENTS.md — web

The workbench UI. Vite + React + TypeScript, Tailwind, components copied into the source
tree rather than installed from a registry at build time -- the target build once this runs
somewhere with real internet access. See "Today" below for what actually exists now
and why it looks different.

## Today: plain HTML/CSS/JS, no build step

`web/src/` today is hand-written `index.html` + `styles.css` + `app.js` -- zero framework,
zero bundler, zero `node_modules` -- served directly by `services/api`'s Starlette app via
`StaticFiles`, same origin as the API (no CORS configuration exists because there is
nothing cross-origin to allow). Two independent reasons, not one: the sandbox this was
first built in cannot `npm install` anything from the registry (403 from
`registry.npmjs.org`, confirmed empirically), and separately, even a globally-cached React
build turned out to be CommonJS-only with no UMD browser bundle, so a bundler-free `<script>`
tag was never on the table regardless of network access. This is an interim, sandbox-driven
substitution, not a change of direction -- the Vite+React+Tailwind build above is still the
real target for the WSL2 machine (ADR-0005), which has ordinary internet access.

What it covers is a single-page app (`app.js`, hash routes, no dependencies) with a
workbench and eight further views.

**The workbench** (`#/work`, [ADR-0008](../docs/adr/0008-a-workbench-where-agents-and-people-write-reports-together.md)) is an IDE for reports, laid out
as the vision image drew it. Its code is in `workbench.js` (the layout and the command
line) and `workbench-tabs.js` (one object per tab kind: `{title, icon, render}`). The two
files share `app.js`'s helpers through `window.Citadel`.

- **Left.** The resource tree: every document the person may read, by folder and version,
  and the database tables. Their reports. The tools running now, and a Tools panel with
  what is available, what is active and the history.
- **Centre.** Tabs and a resizable command line (`/help` lists the commands).
  - **Welcome**: write a report from a prompt.
  - **Task file** (`/task`): write a long request, then Commit.
  - **Task**: agent cards (click one for its goal, current step, completed work, tools and
    what it waits for), the live journal, shared state and a note form.
  - **`agent_N.plan`**: one agent's plan.
  - **Report**: the editor. Every template section is editable, including headings and
    paragraphs, with add, move and remove a section. Click evidence to insert its id.
    **Save as vN** (or Ctrl+S) renders and verifies a new version. **Revise** sends the
    report back to the agents. A version picker, preview and download. Unsaved edits are
    flagged. They are never lost silently: switching version, closing the tab, leaving
    the workbench (`app.leaveGuards`) or the page each stop and say so, and **Discard**
    returns to the saved version.
  - **Document**: its versions and what changed between them.
  - Observability panels, sandbox state and memory.
- **Right.** Observability, metrics and evaluation. The environment and sandbox-state
  tree. The activity feed, in words.
- The open tabs last for the browser session (`sessionStorage`); the active tab is in the
  URL (`#/work/<tab>`), and the command line's height is remembered in `localStorage`.
  Every storage call is wrapped, and the page works without storage.

The other views:

- **Task log** (`#/tasks/<id>`, the original task view). Submit a goal at a chosen
  classification and follow the task live. The
  journal streams over SSE, read with `fetch()` so that the bearer token travels in a
  header and never in a URL. The view shows the plan, each step's routing breakdown, tool
  calls and their results rendered by output shape, citation chips that open the source
  region, budgets, cancel, the deliberate probe, the per-task sovereignty report, and the
  deliverable with preview and download.
- **Documents.** Upload with a declared classification and ACL, watch the ingest status,
  and see each page image with its blocks.
- **Search & access.** One query as all three identities side by side, with the denials
  counted: the ACL surface below.
- **Approvals.** The approver's queue, the verification report per tier, approve or
  reject with a comment, and the released artifact with its hash and provenance.
- **Models & routing.** The runtime's inventory, *Pull missing models*, and the router
  run live with each candidate's score.
- **Sovereignty.** The panel below.
- **Audit & policy.** The hash-chained log with chain verification, and the policy
  evaluator for trying a decision by hand.
- **Metrics.** Latency and error rates, and the traces.

The three acceptance-target surfaces in the table below are all built. Every asset is
served from the API's own origin, and `tests/structural/test_no_external_urls_in_build.py`
checks that no external host appears in any of them.

## The binding constraint is the build, not the runtime

Handoff §3.2 requires no internet at **build** time. That rules out any component library
resolved from a network registry during a build, and it is why components are copied into
the repo (shadcn/ui-style) — they become ordinary reviewable files that exist on a
disconnected machine because they are committed.

- Commit the lockfile. `npm ci` against a vendored tarball cache.
- Self-host fonts and icons. **No CDN reference anywhere**, including in a comment that
  someone will later uncomment.
- `tests/structural/test_no_external_urls_in_build.py` greps the built assets and fails on
  any external host. It is also evidence for acceptance target E.

## Streaming is SSE

One-directional, reconnects on its own, survives proxies, and the architecture already says
HTTP + SSE. Cancellation is an ordinary POST. Do not reach for WebSockets without a
superseding ADR.

## Surfaces that are acceptance targets, not nice-to-haves

Three parts of this UI are scored directly. They need to be designed as demonstrations, not
as log views.

| Surface | Serves | Must show |
|---|---|---|
| **Routing breakdown** | Target A | Which model, and the per-candidate score breakdown including the residency term. A log line does not count. |
| **Citation highlight** | Target D | Click a citation → the exact region of the scan, by page and bounding box |
| **Sovereignty panel** | Target E | Attempts / blocked / bytes out, live, plus the deliberate-probe button and the per-task export |

Plus the ACL surface that ADR-0001 §Q7 calls for: two engineers, different clearances, the
same query, visibly different citation sets **and the denial count**. ACL filtering is
invisible when it works, which is why it needs a surface built for it.

## Live task view

Current step, elapsed, budget consumed, model in use, and **queue wait shown separately**
so a task waiting on GPU admission reads as waiting rather than as hung.
