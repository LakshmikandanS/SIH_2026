# ADR-0006 — One command on Windows: Docker Desktop runs the stack, and the egress ruleset lives in each container

**Status:** Accepted · **Date:** 2026-09-23
**Amends:** [ADR-0005](./0005-wsl2-execution-environment.md) on *which Docker runs Citadel*. The
ADR-0005 configuration stays supported, unchanged, and is still the stronger one.
**Follows from:** [ADR-0004](./0004-the-demonstration-runs-on-one-box.md) (one box, and the cable)

---

## Context

ADR-0005 chose a dedicated WSL2 distro with Docker Engine installed natively, and ruled
Docker Desktop out, for one reason: the enforcement it expected to rely on was a
**host-level** nftables ruleset. Inside Docker Desktop's managed VM there is no host the
user administers; rules added there sit among Docker Desktop's own, may not survive a
restart, and are not something to show an observer as "the firewall".

Building the enforcement changed that premise.

1. **The ruleset lives in each container, not on the host.**
   `ops/compose/egress-entrypoint.sh` installs `ops/nftables/citadel-egress.nft` —
   default-deny `OUTPUT`, allowing loopback, replies, the deployment's internal networks
   and the inference port — in the container's **own network namespace**, before the
   application starts. It then drops to uid 10001 with an empty capability bounding set
   and `no_new_privs`, so the application can never change the rules. A container's
   network namespace is the same kind of object under every Docker: Docker Engine in a
   WSL2 distro, Docker Desktop, or bare-metal Linux. The rules are reapplied at every
   container start, so a restart cannot lose them. They can be inspected
   (`docker compose exec api nft list ruleset`), and the sovereignty panel shows their
   exact text. They are Citadel's own rules, not Docker's. ADR-0005's objection was to
   host-level rules in a VM nobody administers, and it does not apply to these.
2. **The rest of the enforcement never depended on the host.** Postgres and the sandbox
   sit only on `internal: true` networks. Docker creates those without a gateway, so there
   is no route out of them, whatever any ruleset says.

The requirement is concrete too: **one command, on the demonstration machine, which runs
Windows.** Docker Desktop and Ollama for Windows are two ordinary installers. ADR-0005's
setup needs a new distro, a Docker Engine install and the NVIDIA container toolkit. It also
needs the Blackwell-on-WSL2 GPU-passthrough check, which ADR-0005 itself calls a known
trouble spot and a blocking one.

## Decision

**`citadel.cmd` runs the Compose stack (`ops/compose/docker-compose.yml`) on Docker
Desktop. Ollama runs natively on Windows**, where it has the GPU through the ordinary
Windows driver, so there is no passthrough question. The containers reach it at
`host.docker.internal:11434`. That address and port are the only destination outside the
deployment that the container ruleset allows.

**The ADR-0005 configuration stays supported, with the same Compose file.** Run
`scripts/up.sh` from inside a WSL2 distro that has Docker Engine and Ollama
(`OLLAMA_HOST=0.0.0.0`, so the containers can reach it). Nothing in the repository
branches on which of the two is in use.

## What this gives up

**The Docker host is outside every rule Citadel applies.** Under Docker Desktop that host
is Windows, and the inference runtime runs there. The containers can reach one port on
the host and nothing else. Ollama's own egress, however, is governed by Windows, not by
this ruleset: a model pull, or the Ollama tray app's update check. The sovereignty panel
says this in words ("Outside these rules", from the entrypoint's own statement in
`/run/citadel/enforcement.json`), so that "nftables applied" never implies more than the
rules cover. The ADR-0005 configuration has the same gap one level down: nobody has yet
written a ruleset for the WSL2 distro itself (see *Revisit when*).

What covers the host is what ADR-0004 said would make target E convincing in the first
place: **the cable.** Pull the models once. Then disconnect the network and run every
flow. Nothing degrades, because nothing was ever reaching out. The container rules and
the telemetry make the claim auditable on screen. The disconnected cable makes it
physical, and it covers the host as well.

On a machine that stays connected, one optional hardening step is a Windows Firewall
outbound block on the Ollama executables, lifted only while pulling. The paths below are
Ollama for Windows' default per-user install; check them on the machine:

```powershell
# PowerShell as Administrator
$ollama = "$env:LOCALAPPDATA\Programs\Ollama"
New-NetFirewallRule -DisplayName "Citadel: Ollama egress" -Direction Outbound -Action Block -Program "$ollama\ollama.exe"
New-NetFirewallRule -DisplayName "Citadel: Ollama app egress" -Direction Outbound -Action Block -Program "$ollama\ollama app.exe"
# to pull a model later: Disable-NetFirewallRule -DisplayName "Citadel: Ollama*"   (Enable- afterwards)
```

Connections from the containers reach Ollama inbound, through Docker Desktop's own process
on the host's loopback. An outbound block on the Ollama executables should therefore stop
Ollama reaching out without stopping Citadel reaching Ollama. Check this after adding the
rules: `citadel status` should still report every model installed.

## Consequences

- `citadel.cmd` is the demonstration path: build, start, wait for health, open the
  browser. `citadel models` pulls the registry's approved tags through the host's Ollama.
  `scripts/up.sh` is the same stack on Linux or WSL2. `scripts/run.sh` is the
  no-container developer path, and its sovereignty panel says, correctly, that no
  network-level enforcement is present.
- The entrypoint needs `NET_ADMIN` (to install the rules), `SETUID`/`SETGID` (to switch
  user) and `SETPCAP` (to empty the bounding set), and nothing else. Without `SETPCAP` the
  containers never start. That was found by running the entrypoint under exactly that
  capability set, in a scratch network namespace, before it ever reached Docker.
- The HTTP clients for the inference runtime and the sandbox ignore proxy environment
  variables (`trust_env=False`). Docker Desktop injects `HTTP(S)_PROXY` into containers
  whenever a proxy is configured, and honouring one would carry prompts and document
  excerpts off the box. Both clients have a regression test against a dead proxy port;
  each test fails if the setting is reverted.
- The connection scanner counts only connections a process opened, not ones accepted on
  its own listening ports. Docker delivers a published port's connections from an
  address on the `host_link` network, which is outside every network the deployment
  trusts. Counted as egress, the operator's own browser would have read as data leaving.
  A test covers both directions of one connection.
- ADR-0005's GPU-passthrough check is no longer on the one-command path, because Ollama
  for Windows uses the Windows driver directly. It still applies to the WSL2
  configuration.
- PLAN-M0 task 12's "a container cannot reach the internet" holds under both
  configurations. Its "the sandbox has no interface at all" does not, for a separate
  reason: see [ADR-0007](./0007-the-sandbox-is-a-service-not-a-container-per-run.md).

## Revisit when

- **The claim has to hold on a connected network without relying on the host.** Then run
  the ADR-0005 configuration and write the distro-level ruleset it still lacks:
  default-deny `OUTPUT` for the distro, allowing loopback, the Docker bridges and replies.
- **Docker Desktop's kernel refuses nf_tables inside a container namespace** on the
  demonstration machine. The entrypoint then reports "could NOT be applied" on the panel,
  and `CITADEL_REQUIRE_ENFORCEMENT=1` makes the containers refuse to start. At that point
  the ADR-0005 configuration is the answer, not a workaround.
