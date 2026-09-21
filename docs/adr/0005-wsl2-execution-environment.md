# ADR-0005 — Egress enforcement and the whole stack run inside a dedicated WSL2 distro

**Status:** Accepted · **Date:** 2026-09-21
**Resolves:** the open question in ADR-0004 ("what OS does the demonstration machine run?")
**Amends:** ADR-0004 §"Open: what OS" — that section's table entries stand; this picks between them

---

## The answer

The demonstration machine is **Windows, with Docker Desktop already installed, and WSL2
available.**

That is ADR-0004's middle row, not its best or worst. It needs one more decision the ADR
didn't make: Docker Desktop *has* a WSL2 backend already, so "use WSL2" is ambiguous
between two real setups that behave very differently for the one thing that matters here —
whether nftables rules actually govern the containers' network.

## The two things "WSL2" could mean, and why they are not the same

**Option A — Docker Desktop, WSL2 backend (the default, already installed).**
Docker Desktop's containers do run inside a WSL2 VM, but it is Docker Desktop's *own*
managed distro (`docker-desktop`, plus `docker-desktop-data`) — not one the user
administers. Its networking is Docker Desktop's own NAT/vpnkit-descended stack layered on
top of WSL2, integrated with Windows Defender Firewall on the host side. Docker Desktop
manages the iptables/nftables rules inside that hidden distro itself, for its own port
forwarding and container networking. Rules added alongside that are not guaranteed to
survive a Docker Desktop restart, and are not the rules an observer would be shown as "the
firewall" — they would be reading Docker Desktop's internals.

**Option B — a dedicated WSL2 distro, with Docker Engine installed natively inside it.**
`wsl --install -d Ubuntu-24.04`, then `docker-ce` installed inside that distro directly, the
way it would be installed on any Linux host. This is an ordinary Linux network namespace
that the user fully controls. nftables behaves exactly as `packages/sovereignty/AGENTS.md`
and `ops/nftables/` already assume, because it *is* the thing they assume — a real Linux
box, which happens to be virtualised under Windows.

## Decision

**Option B.** A dedicated WSL2 distro (`citadel` or similar, based on Ubuntu 24.04) runs
Docker Engine natively, and the entire stack — Postgres, the API, workers, the sandbox
runner, Ollama, and the nftables ruleset — runs inside it. Docker Desktop stays installed
for whatever else the user uses it for, but **it does not run Citadel.**

This is exactly what ADR-0004 meant by "treat the WSL2 VM as the box" — that sentence only
holds if the VM in question is one whose network namespace is actually under user control,
which rules out Option A.

### Why this doesn't cost what it sounds like it costs

- One `wsl --install` and one `apt install docker-ce` inside it. Not a new machine.
- `docker compose up` and every command in `docs/PLAN-M0.md` runs unchanged — they are run
  *from inside* the WSL2 distro's shell, which is an ordinary Linux shell.
- **The cable demonstration from ADR-0004 still works.** Disabling the network adapter WSL2
  uses (or disconnecting the host's network entirely, since WSL2's NAT depends on the host
  having a route) cuts the VM off exactly as unplugging a physical machine would.
- Nothing else in any ADR changes. `demo-local` is still one box; this ADR just says which
  box.

### GPU passthrough — verify this first, not last

CUDA-on-WSL2 works through the **Windows-side NVIDIA driver** (WSL2 CUDA support has been
standard since driver 470+): install the Windows driver, and do **not** install a separate
Linux NVIDIA driver inside the distro — only `nvidia-container-toolkit`, so
`docker run --gpus all` and Ollama can see the card.

The RTX 5060 is Blackwell (sm_120) and needs a current driver with CUDA 12.8+ support.
ADR-0002 already flagged Blackwell-on-WSL2 as a known trouble spot. **Before building
anything else on this decision, from inside the WSL2 distro:**

```bash
nvidia-smi                       # the card must be visible from inside the distro
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

If either fails, that is a blocking finding for this ADR, not a detail to work around
quietly — it would mean falling back to Option A with a materially weaker sovereignty
story, or running Ollama on the Windows side and only the rest of the stack in WSL2, which
reopens the "one box" argument from ADR-0004. Confirm this before M0 task 12.

## Consequences

- `docs/PLAN-M0.md` task 12 is **unblocked**.
- `ops/nftables/` is written against a real Linux network namespace with no caveats.
- Add a short `ops/wsl2/README.md`: the distro setup, Docker Engine install, and the
  `nvidia-container-toolkit` step — the mechanical instructions this ADR's decision implies.
- The Windows host itself is still outside the ruleset, same as it would be outside any
  hypervisor's guest — but nothing runs there. All egress-capable processes are inside the
  distro. This is the same trust boundary a bare-metal Linux box would have.
- `registry/profiles.yaml`'s `demo-local.inference.endpoint` (`host.docker.internal`) is
  unaffected: Docker Engine inside the WSL2 distro resolves `host.docker.internal` to the
  distro's own host-side gateway, which is what Ollama binds to whether Ollama runs as a
  native WSL2 process or in its own container. No change needed there.

## Revisit when

- Docker Desktop ships a supported way to run user-defined nftables/iptables rules inside
  its own WSL2 backend that survive restarts and are inspectable — unlikely soon, and not
  worth waiting for.
- The GPU passthrough check above fails, in which case this ADR is wrong and needs to be
  superseded with whatever workaround is required — do not patch around a failed check
  silently.
