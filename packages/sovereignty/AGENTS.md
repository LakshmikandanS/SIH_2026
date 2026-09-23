# AGENTS.md — sovereignty

Acceptance target E. The proof of the entire product claim.

## Depends on

`contracts`, `platform`.

## Two halves, deliberately independent

| | |
|---|---|
| **Enforcement** | Default-deny nftables ruleset in each egress-capable container's own network namespace, internal-only Docker networks, internal DNS — plus an in-process fence in every Python process |
| **Telemetry** | A monitor that observes and records attempts **regardless of the firewall** |

They must not share an implementation, and the telemetry must not read the firewall's logs.
A firewall alone proves nothing to an observer — you cannot see it working. Telemetry alone
enforces nothing. **The pair is the evidence**, and it is only evidence because either one
failing would be visible in the other.

Network enforcement lives in `ops/nftables/citadel-egress.nft`, applied per container by
`ops/compose/egress-entrypoint.sh` (ADR-0006). This package is the in-process fence, the
telemetry, the reporting and the probe.

## What telemetry records

Attempts, blocks and DNS denials, each **attributed to a task and an agent**. Attribution
is what makes the report useful; an unattributed counter is a number with no story.

A rule an observer can read in five seconds is better evidence than a thorough one they
cannot: `citadel-egress.nft` is a dozen lines, and the panel shows its exact text. With
one box (ADR-0004) the old app-box ↔ GPU-box link is gone. What stands in its place is the
container ↔ host link to the inference runtime: exactly one address and port are allowed,
and the host itself is outside the ruleset. The panel says so (ADR-0006).

## Surfaces

- **Live panel:** attempts, blocked, bytes out. Reads zero during a normal task.
- **Per-task report:** exportable, attributable, part of the provenance record.
- **The deliberate probe:** a button that makes the sandbox attempt an outbound HTTPS call.
  The firewall blocks it, the monitor records `CONNECT_BLOCKED` with attribution, the
  counter increments, **and the task continues unharmed.** That last part is the
  demonstration — a system that survives the probe is a system where enforcement is real
  and not load-bearing on good behaviour.

## Build-time egress counts too

Handoff §3.2 requires no internet at **build** time either. The frontend build embeds no
external URL — no CDN, no font host, no icon service — and a structural test greps the
built assets and fails on any hit. That test is also evidence for target E.

## Built

- **`EgressMonitor`** is an audit-hook observer. It compares every outbound
  `socket.connect` and every name lookup with the deployment's own destinations
  (`EgressPolicy.from_env`), and records anything else as an `attempt`, attributed to the
  task and agent active at that moment. It is registered before the fence, so it sees an
  attempt whether or not the fence then refuses it.
- **`ConnectionScanner`** asks the operating system (psutil), not Python, which
  connections the process and its children hold. Anything established to an external
  address is recorded as `observed`: the number that must read zero, and the only
  detector that would see a native library opening its own socket.
- **`EgressFence`** is the in-process refusal (`EgressBlocked`). It is defence in depth
  under the network rules, and it is the only enforcement when there are no containers
  (`scripts/run.sh`). The panel says which of the two situations it is in.
- **The deliberate probe** (`run_probe`) steps around the fence on purpose, so that what
  it reports is what the network did. It runs from the API process and from inside the
  sandbox (`POST /api/sovereignty/probe`, or per task).
- **`EgressRecorder`** is the only writer. A hook only puts to a queue; a background
  thread writes `egress_events` and, for the governance-relevant kinds, the audit chain.
- **Reports.** `status()` feeds the live panel and `task_report()` gives the per-task
  record that is part of provenance. `enforcement_status()` returns the network half's
  own statement, `/run/citadel/enforcement.json`, written by the entrypoint. The report
  shows both halves side by side and never infers one from the other.

Not measured: bytes out. The panel counts attempts, blocks, DNS denials, probes and
observed connections. "Observed external connections: 0" is the claim, and bytes would
only restate it.

## Resolved: where the GPU cluster is

ADR-0003 settled it. The cluster is a measuring instrument that never sees confidential
material and is never on the demonstration path. Target E is demonstrated on the one
local box, where the inference runtime is on the premises.
