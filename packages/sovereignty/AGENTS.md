# AGENTS.md — sovereignty

Acceptance target E. The proof of the entire product claim.

## Depends on

`contracts`, `platform`.

## Two halves, deliberately independent

| | |
|---|---|
| **Enforcement** | Default-deny nftables ruleset, internal-only Docker networks, internal DNS, sandbox with no interface |
| **Telemetry** | A monitor that observes and records attempts **regardless of the firewall** |

They must not share an implementation, and the telemetry must not read the firewall's logs.
A firewall alone proves nothing to an observer — you cannot see it working. Telemetry alone
enforces nothing. **The pair is the evidence**, and it is only evidence because either one
failing would be visible in the other.

Enforcement lives in `ops/nftables/`. This package is the telemetry, the reporting and the
probe.

## What telemetry records

Attempts, blocks and DNS denials, each **attributed to a task and an agent**. Attribution
is what makes the report useful; an unattributed counter is a number with no story.

With two boxes (ADR-0001 §Q2) there are two enforcement points and the GPU box's ruleset is
trivially auditable: one inbound port from the app box, zero outbound, no DNS. A rule an
observer can read in five seconds is better evidence than a thorough one they cannot.

**The app box ↔ GPU box link is in scope.** It is an internal boundary, not an exempt one.

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

## ⚠️ Open question that changes this package's story

**Where is the GPU cluster?** See ADR-0002. If the 250 GB allocation is off-premises, then
confidential text crosses a network the organisation does not own on every model call, and
target E cannot honestly be demonstrated on that configuration — the panel would be
measuring the app box while the real egress is the inference call. Until confirmed, treat
the cluster as off-premises and demonstrate target E on the local box.
