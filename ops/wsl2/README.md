# WSL2 setup for `demo-local`

Resolves [ADR-0005](../../docs/adr/0005-wsl2-execution-environment.md): the whole stack runs
inside a dedicated WSL2 distro, with Docker Engine installed natively there, never inside
Docker Desktop's own managed WSL2 backend. [ADR-0006](../../docs/adr/0006-docker-desktop-launcher-and-per-container-enforcement.md)
made Docker Desktop the one-command demonstration path. This configuration stays
supported, with the same Compose file, and is still the stronger one.

**The complete, step-by-step guide is [SETUP.md, Part 2](../../SETUP.md#part-2--the-wsl2-distro).**
On Windows, `setup wsl2` does all of it: it checks the Windows side, creates the distro,
provisions it, and starts Citadel.

## `provision.sh`

[`provision.sh`](./provision.sh) is what `setup wsl2` runs inside the distro. It can also be
run by hand from inside it:

```bash
bash /mnt/c/AI_WORKBENCH/SIH_2026/ops/wsl2/provision.sh --check   # report only
bash /mnt/c/AI_WORKBENCH/SIH_2026/ops/wsl2/provision.sh           # do it
```

Each step is skipped when it is already done:

1. **systemd** is enabled in `/etc/wsl.conf`; exit status 3 means "restart the distro, then run again".
2. **The GPU** is visible inside the distro (`nvidia-smi`), through the **Windows** driver. No
   Linux NVIDIA driver is installed here. This check is blocking.
3. **Base packages**: `ca-certificates curl git gnupg nftables`.
4. **Docker Engine** is installed from get.docker.com, the service is enabled, and the user is
   added to the `docker` group. The script refuses to continue if `docker` is Docker
   Desktop's WSL integration.
5. **The NVIDIA Container Toolkit** is installed, and `docker run --gpus all nvidia/cuda:…
   nvidia-smi` must see the card. This check is also blocking. `--no-container-gpu` skips it.
6. **Ollama** is installed natively as a systemd service, with `OLLAMA_HOST=0.0.0.0:11434`
   (a drop-in in `/etc/systemd/system/ollama.service.d/`), because the containers reach it
   through the Docker bridge's gateway (`host.docker.internal` → `host-gateway`). In
   WSL2's default NAT networking that address is still unreachable from other machines.
   Mirrored networking would expose it.
7. **The repository** is cloned into `~/citadel` from the Windows folder, which keeps Linux
   line endings and the scripts' execute bits, or fast-forwarded if already there.

Then, in a new shell (so the `docker` group applies):

```bash
cd ~/citadel && scripts/up.sh && scripts/up.sh models
```

and open http://127.0.0.1:8000 in a browser on Windows.

## The cable test still works

Disabling the network adapter on the Windows host, or physically disconnecting it, cuts
the distro off the same way unplugging a physical machine would, since WSL2's NAT depends
on the host having a route out. That keeps the ADR-0004 demonstration literally true
here: disconnect the network, then run the whole flow.
