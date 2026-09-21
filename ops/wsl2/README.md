# WSL2 setup for `demo-local`

Resolves ADR-0005. Read that ADR first — this is the mechanical follow-through, not the
justification.

**The whole stack runs inside a dedicated WSL2 distro with Docker Engine installed
natively — never inside Docker Desktop's own managed WSL2 backend.** Docker Desktop can
stay installed for anything else; it does not run Citadel.

## 1. Create the distro

```powershell
# Windows PowerShell (admin)
wsl --install -d Ubuntu-24.04
wsl --set-default Ubuntu-24.04
```

If a distro already exists and you want a clean one for this project:

```powershell
wsl --install -d Ubuntu-24.04 --name citadel
```

## 2. NVIDIA driver — Windows side only

Install or update the **Windows** NVIDIA driver (not a Linux driver) to a version with WSL2
CUDA support (470+; get current for Blackwell). Do **not** install `nvidia-driver-*` inside
the distro — only the container toolkit, below. Installing a Linux driver inside WSL2 is
the most common way to break GPU passthrough there.

## 3. Inside the distro: Docker Engine (not Docker Desktop)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# new shell, or: newgrp docker
```

## 4. Inside the distro: NVIDIA Container Toolkit

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## 5. Verify GPU passthrough — do this before anything else in M0 task 12

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

Both must show the RTX 5060. If either fails, **stop** — ADR-0005 names this a blocking
finding, not a detail to work around. Do not proceed to nftables or Compose until this
passes.

## 6. nftables

`sudo apt-get install -y nftables` — this is a real Linux network namespace, so
`ops/nftables/*.nft` applies with no WSL2-specific caveats. Load it the same way you would
on bare metal.

## 7. Everything else

From here, every command in `docs/PLAN-M0.md` and the root `README.md` runs from an
ordinary shell **inside this distro**:

```bash
cd ~/citadel        # wherever you clone/copy the repo inside the distro
uv sync
docker compose up
```

`host.docker.internal` inside containers resolves to the distro's own host-side gateway —
Ollama running natively in the distro (or in its own container) is reachable through it
with no change to `registry/profiles.yaml`.

## The cable test still works

Disabling the network adapter on the Windows host (or physically disconnecting it) cuts the
distro off the same way unplugging a physical machine would, since WSL2's NAT depends on
the host having a route out. This is what makes the ADR-0004 demonstration — disconnect the
network, run the whole flow — still literally true here.
