# M0 build notes: how the foundations got here (history)

This is the running "Current state" narrative that root `AGENTS.md` carried while M0's
foundations were being built, moved here verbatim on 2026-09-23 when the system became
runnable end to end. It is history: the bugs each step found, and why several conventions
in this repository look the way they do. It is **not** the current state. That lives in
root `AGENTS.md`, and where the two disagree, `AGENTS.md` is right.

---

M0 is underway. `citadel_contracts` (`docs/PLAN-M0.md` task 3) is written, ported from the
prototype per `packages/contracts/AGENTS.md`, and verified. The structural test suite
(`docs/PLAN-M0.md` task, `tests/structural/AGENTS.md`) is also written: all ten detectors
it owes, each with a negative control and a not-vacuous check. `citadel_platform`'s
registry loader (`docs/PLAN-M0.md` task 4) is written: every `registry/*.yaml` file loads
under strict pydantic validation (unknown field, unknown classification, duplicate id, a
`fallback`/policy-operator typo -- all fail loudly, naming the file and, for a top-level
entry, the source line), for both profiles. Building it found two more registry bugs of
the same shape as the `classification_ceiling: restricted` one already fixed in
`profiles.yaml`: `models.demo-local.yaml` and `tools.yaml` both used `restricted`, which
`citadel_contracts.classification.Classification` has never defined -- fixed in those
files' own header comments. Postgres migrations and the audit chain (the rest of task 19,
`docs/PLAN-M0.md` tasks 5 and 6) are now built and empirically proven, closing task 19.
`packages/platform/migrations/0001`-`0004` cover the audit chain, identity/tasks/journal,
artifacts/approvals/registry-state, and pgvector document chunks; `citadel_platform.
migrations` applies and reverts them via a `psql` subprocess runner (no Postgres Python
driver needed, so it runs for real in this sandbox). The audit chain is
`citadel_platform.audit.chain` (pure-Python hashing and `verify()`, unit-tested with an
in-memory fake and a negative control for every way a chain can be broken) plus
`citadel_platform.audit.postgres` (the real psycopg writer/reader — type-checked here,
executable once a driver exists on the target machine). What actually delivers "one
logical writer" is migration 0001's `BEFORE INSERT` trigger, proven against a real
Postgres via `psql`/`subprocess` in `packages/platform/tests/test_audit_chain_schema.py`:
20 concurrent writers produce a gapless, unbroken chain; the app role cannot `UPDATE`/
`DELETE`; `verify()` catches a row edited after the anti-tamper trigger is disabled. That
proof caught a real bug on the way: `seq` as `GENERATED ALWAYS AS IDENTITY` is assigned
*before* a trigger runs and sequences do not block on each other, so two concurrent
writers could commit in the opposite order from the one they grabbed a `seq` value in —
confirmed by two genuine broken links under 10-way concurrency. Fixed by dropping
`IDENTITY` and having the trigger assign `seq` itself, from the same locked read that
determines `prev_hash`. `test_audit_chain_postgres.py` (the psycopg-driven counterpart)
and `test_vector_iterative_scan.py` (pgvector's `hnsw.iterative_scan`) are real,
reviewed, currently-collected-and-honestly-skipped code, gated on a driver and an
extension this sandbox cannot install — not deleted, not faked; they run on the WSL2
machine. `scripts/dev-db.sh` (below) is the prerequisite all of this package's Postgres
tests share.

Identity, roles, clearances and ACLs (`docs/PLAN-M0.md` task 7, the first sub-scope of
task 20) is now built and empirically proven. `citadel_contracts.identity` signs and
verifies session tokens — Ed25519 via PyJWT, shaped after `receipts.py` on purpose and
deliberately independent from it (distinct `TOKEN_TYPE`, distinct `SIGNING_ALGORITHM`
constant, never a shared import — `packages/contracts/AGENTS.md`'s new section), with
`citadel_contracts.domain.User` itself as the output type rather than a parallel
identity shape. `citadel_platform.identity.keys` owns the key lifecycle `identity.py`
deliberately does not: read `CITADEL_SESSION_SIGNING_KEY`, or fail loud-but-not-closed
with a fresh random per-process key, on the reasoning in that module's own docstring.
Migrations 0005 (a singular `role` column, `CHECK`-constrained to
`engineer`/`approver`/`admin` — singular by design, since `registry/policy.yaml`
branches on a singular `actor.role` even though `User.roles` is a tuple) and 0006 (the
three ADR-0001 §Q7 demo identities: two engineers at different clearances in different
departments, one approver) seed the Postgres side. `test_identity_seed.py` proves the
two engineers actually differ in both clearance and department rather than trusting the
`INSERT` by inspection, and proves 0006-then-0005's `down` migrations undo exactly what
they added. `test_identity_not_from_body` (already in the structural suite) continues to
pass — task 7's other "Done" clause.

Smoke-testing the new `python -m citadel_platform.identity genkey` CLI surfaced a real,
previously-unverified bug that predates this task: no package anywhere in the repo had a
`__main__.py`, so `python -m citadel_platform.<pkg>` — the exact invocation
`migrations/cli.py`'s own docstring and `argparse` `prog=` document — has never actually
worked, for `migrations` either, since task 5. Only `python -m
citadel_platform.migrations.cli` or an in-process `main()` call ever ran. Fixed for both
packages with a two-line `__main__.py` delegating to `cli.py`'s `main()`;
`packages/platform/AGENTS.md` now carries a standing note so a future CLI subpackage
does not repeat it. Neither `scripts/check.sh` nor any existing test caught this, because
pytest never shells out to a CLI via `-m` — confirmed fixed by re-running both
invocations for real (`genkey` against no database, `migrations status` against the dev
Postgres), not just by re-running the test suite.

Combined at that point: 197 tests pass, 2 skipped (101 contracts + 31 structural + 65
platform), `mypy --strict` clean across 60 source files, `ruff check` clean — all with the
disposable Postgres `scripts/dev-db.sh start` brings up, so the 2 skips are only the
psycopg/pgvector gaps above, never a missing database. Getting the migrations and audit
chain landed also fixed three real cross-package
tooling gaps that only show up once a second package has tests, two now and one earlier:
pytest's default import mode collided on two different `tests/` directories both resolving
to the bare module name `tests` (fixed by dropping `__init__.py` from every `tests/`
directory, repo-wide, and running pytest with `--import-mode=importlib` instead -- see the
comment on `addopts` in root `pyproject.toml`), mypy hit the identical collision under its
own module resolution (fixed by listing each test module individually in
`tool.mypy.overrides`, the same file), and mypy hit the same collision a third time over
two different `conftest.py` files (`tests/structural/` and a would-be
`packages/platform/tests/`) -- unfixable by another override, since this time it was two
*different* files both wanting to be the one module `conftest`, not one file needing
different settings. Fixed by not using a second `conftest.py` at all:
`packages/platform/tests/pg_scratch.py` is a plain, uniquely-named module, imported
explicitly by the tests that need it, with `packages/platform/tests` added to root
`pyproject.toml`'s `pythonpath` so it resolves. The other seven packages' `tests/__init__.py`
stubs had been left in place when the first fix landed -- inconsistent with the "no
`__init__.py` under any `tests/`" invariant it established, and a silent reintroduction of
the same collision waiting for whichever package's tests are written next -- found and
deleted while building `scripts/check.sh` below, which is what now runs across all nine
packages, not just the two with tests today.

The policy evaluator (`docs/PLAN-M0.md` task 8, first half) is now built and proven
against the real registry. `citadel_tools.policy.evaluate(rules, actor=, resource=,
tool=, receipt=) -> Decision` is a pure function — no I/O, no audit-chain write —
implementing `registry/policy.yaml`'s ordered, first-match-wins, default-deny semantics
over its five operators. `resource` and `tool` are `citadel_contracts.domain.Resource`
and `citadel_platform.registry.schema.ToolEntry` directly, since both already carry
exactly the fields the rules reference; `actor` needed one new shape, `ActorFacts`,
because `User` carries `roles`, not `capabilities` — closed by a new registry file,
`registry/roles.yaml` (role → capability grants: engineer/admin get the full working
set, approver deliberately gets only `retrieval`, so an approver cannot `fs.write`/
`code.run`/`doc.generate` even by mistake — capability-enforced, not just
convention-enforced), and by `actor_facts_from_user()`, the one function that turns a
`User` into an `ActorFacts` and does the two boundary normalisations that requires
(reject more than one role; uppercase `clearance`) rather than leaving them implicit.
`test_policy.py` has one test per real rule in `registry/policy.yaml`, in file order,
plus the default-deny case no rule in the file encodes — task 8's first two `Done`
clauses. Its third — every decision, allow and deny alike, produces an audit event —
waits on the chokepoint below, since the evaluator itself never writes anything.

Building it found two more real bugs, same shape as the `__main__.py` one above:
documented behaviour the code never actually implemented, caught by being the first
genuine consumer. First, in `citadel_platform`: `_known_classification()`
(`registry/schema.py`) validated a registry classification value via
`Classification.rank(value.upper())` but returned the original, un-uppercased `value` —
so every `classification_ceiling` read off the registry silently kept its as-written
lowercase YAML casing instead of becoming the lattice's uppercase form, contradicting
the function's own docstring. Nothing had ever called `Classification.rank()`/
`.exceeds()` on a registry-sourced value before the evaluator's `exceeds` operator did,
which is exactly why eight of the evaluator's own tests were the ones to catch it
(`ValueError: unknown classification 'confidential'`). Fixed by uppercasing before
returning, not only before validating — `packages/platform/AGENTS.md` has the rest.
Second, in the test infrastructure rather than in code: root `pyproject.toml`'s
per-module mypy override that exempts test files from `disallow_untyped_defs` (test
functions are `def test_x():`, never `def test_x() -> None:`) does not by itself cover
a *typed fixture parameter* (`def test_x(registry: Registry):` — governed by the
separate `disallow_incomplete_defs`) or a *parenthesized* `@pytest.fixture(
scope="module")` (governed by `disallow_untyped_decorators`) — both first used by
`test_policy.py`, for real reasons: a self-documenting fixture type, and a
module-scoped fixture so the real registry loads from YAML once per file, not once per
test. Fixed by adding both flags to the same override, with the reasoning recorded
there rather than only here.

Combined: 236 tests pass, 2 skipped (101 contracts + 31 structural + 68 platform + 36
tools), `mypy --strict` clean across 62 source files, `ruff check` clean — same
disposable-Postgres caveat as above; the 2 skips are still only the psycopg/pgvector
gaps, never a missing database. Task 7 was done already; the evaluator half of task 8
is done as of this paragraph; task 20 was scoped as identity plus ACLs plus the
chokepoint together, and was down to its last third.

Two more Postgres access points now read and write through the same `psql`-subprocess
substitution `citadel_platform.migrations.runner` established for task 5, closing a gap
that had been quietly blocking anything from actually running end to end: nothing before
this could turn a real login into a real `User`, or write a real audit event, without a
driver this sandbox cannot install. `citadel_platform.audit.psql_client.
PsqlChainSource`/`append_via_psql` implement the same `ChainSource` Protocol
`citadel_platform.audit.postgres` does — proven, not assumed, by `test_psql_client.py`
against a real Postgres via `pg_scratch_db()`, including a 5-row chain that
`citadel_platform.audit.chain.verify()` accepts and a byte-identical-formatting pin
against `.postgres`'s own `canonical_json`/`format_occurred_at` (the two modules'
copies must never drift, since both feed the same hash function). `citadel_platform.
identity.store.list_users`/`get_user_by_external_identity` are the read side of task 7
that nothing had needed yet — turning a `users` row back into a `citadel_contracts.
domain.User`, proven against the three real seeded identities. A shared `_psql.py`
holds the one thing both needed and migrations' own `_run_psql` deliberately does not
share: a one-statement-in, parsed-CSV-rows-out runner, `-f -`/script-mode rather than
`-c` (psql's `:'name'` safe-quoting substitution is a script feature, confirmed
empirically after a `-c` attempt failed with a literal `:` syntax error at the backend
parser) with `-q` to suppress a trailing command-completion line `--csv` mode does not
expect (also confirmed empirically, not assumed, after it corrupted a naive parse).

**PLAN-M0 task 10, pulled forward of task 9's tracing/metrics** on Fahim's explicit
request — "make a working project checkpoint as soon as possible... try to complete ui
with working interface" — rather than continuing straight to the tool chokepoint next.
`services/api` (`citadel_api`) is real and running: it wires the registry loader,
session identity, the policy evaluator and the audit chain above into one process with a
real HTTP surface, and `web/src/` (mounted on the same process, same origin) is a real,
working UI over it — sign in as one of the three seeded demo identities, evaluate a real
policy decision against a resource you describe (the ACL demonstration ADR-0001 §Q7
calls for, scoped down to tool-call decisions since retrieval/citations do not exist
yet), and watch it land in a live, hash-verifiable audit log. `scripts/run-api.sh`
starts the whole thing end to end, migrating the persistent `citadel_demo` database on
first run. Proven by running it for real, not only by type-checking it: every endpoint
exercised by hand against a live server, including the two-different-engineers,
same-resource, different-outcome case the ACL demonstration exists to show, plus a
Playwright pass over the actual rendered UI (login → evaluate → audit log → verify
chain → switch identity → reload-keeps-session), all with zero console errors.

Two substitutions made this possible in this sandbox, both temporary and both named
again in the files where they actually matter (`citadel_api`'s and `services/AGENTS.
md`'s own docstrings, `web/AGENTS.md`): **Starlette + uvicorn, not FastAPI** (FastAPI
cannot be installed here — PyPI is network-blocked and only `starlette`/`uvicorn`/
`flask` are pre-installed, confirmed empirically; Starlette is FastAPI's own foundation,
so this is a substitution of implementation, not architecture), and **plain HTML/CSS/JS
with no build step, not Vite+React+Tailwind** (this sandbox's `npm install` is
network-blocked the same way, and even the globally-cached React turned out to be
CommonJS-only with no UMD browser bundle — moot anyway, since invariant 10 forbids a CDN
reference regardless of network access). Neither changes what `services/AGENTS.md` or
`web/AGENTS.md` actually call for; both are reversible the moment this runs on the real
WSL2 machine (ADR-0005) with real internet access. `citadel_api` **is** a real `uv`
workspace member (`services/api/pyproject.toml` declares `starlette`/`uvicorn` as actual
dependencies) — fixed the same day the checkpoint first ran outside this sandbox, once a
machine with real network access exposed the gap: `uv sync` there resolves the workspace
for real rather than falling back to `scripts/lib/env.sh`'s sandbox-bridge `PYTHONPATH`,
and nothing had ever declared `starlette`/`uvicorn` as a dependency of anything, so a
`.venv` built that way had no reason to contain them. `services/*/src` stays in
`scripts/lib/env.sh`'s mypy target discovery regardless — that is about what
`scripts/check.sh` type-checks, not about dependency resolution.

Deliberately not built in this pass, visibly rather than silently: the tool chokepoint
itself and receipt issuance (so `try_policy` always evaluates with `receipt.valid=False`
— an honest reflection of what exists, not a bug, and it takes over the chokepoint's
"record the decision" responsibility in the meantime, explicitly, in its own docstring),
the `worker`/`sandbox` services, and the SSE task stream (there is no task system yet to
stream). **Resume at `docs/PLAN-M0.md` task 8's second half** — the chokepoint, in the
still-empty-of-it `citadel_tools` package (`tests/structural/test_single_chokepoint.py`'s
`TOOL_REGISTRY`/`execute_tool`/`dispatch_tool` naming contract): tool resolution from the
registry, JSON-schema argument validation, dispatch, and turning a `Decision` into an
audit event for both allow and deny — the one piece `try_policy` above stands in for by
hand until it exists.

Combined: 245 tests pass, 2 skipped (101 contracts + 31 structural + 77 platform + 36
tools), `mypy --strict` clean across 72 source files, `ruff check` clean — same
disposable-Postgres caveat as above; the 2 skips are still only the psycopg/pgvector
gaps, never a missing database.

**First real run outside this sandbox found a real bug within minutes.** Fahim ran `uv run
pytest -m structural` directly on the actual Windows machine (not yet inside WSL2) and hit
a test-*collection* crash, not a test failure: `test_vector_iterative_scan.py`'s
`_pgvector_available()` called `subprocess.run(["pg_config", "--sharedir"], ...)` with no
guard for `pg_config` not existing at all -- `FileNotFoundError`, distinct from the
nonzero-exit-code case it did handle -- unlike its sibling `pg_scratch.py`'s
`pg_reachable()`, which already catches exactly that for the identical reason. Collection
runs a module's top-level code at import time, before any `-m` marker filter is applied, so
this crashed the whole file regardless of which marker was selected, on any machine with no
Postgres client tools on `PATH` at all -- this sandbox never caught it because `pg_config`
happens to be pre-installed here. Reproduced first, not just reasoned about: hiding
`pg_config` from `PATH` in this sandbox gave the identical crash, and the same
`try/except (FileNotFoundError, OSError): return False` shape as the sibling function fixed
it -- confirmed by re-running the same hidden-`PATH` scenario and getting a clean "1
skipped" instead. `README.md` had also drifted from reality (it still opened with "M0 has
not started"); it now documents the checkpoint, `scripts/dev-db.sh` / `scripts/run-api.sh`,
and that the shell scripts need a real bash (WSL2, per ADR-0005) even though `uv run
pytest`/`mypy`/`ruff` work directly from a bare Windows shell -- the exact gap this bug was
found through.

