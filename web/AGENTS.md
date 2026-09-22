# AGENTS.md — web

The workbench UI. Vite + React + TypeScript, Tailwind, components copied into the source
tree rather than installed from a registry at build time -- the target build once this runs
somewhere with real internet access. See "M0 checkpoint" below for what actually exists now
and why it looks different.

## M0 checkpoint: plain HTML/CSS/JS, no build step

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

What the checkpoint's UI actually covers: sign in as one of the three seeded demo
identities (no password flow yet), the ACL/policy demonstration below (scoped down from
"citation sets" to "tool-call decisions," since retrieval and citations do not exist yet),
and a live view of the real hash-chained audit log with a "verify chain" button. The three
acceptance-target surfaces in the table below -- routing breakdown, citation highlight,
sovereignty panel -- are none of them built yet; they need the model gateway, retrieval, and
sovereignty telemetry this checkpoint deliberately did not reach ahead of getting something
real running end to end first.

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
