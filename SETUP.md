# Setting up Citadel

This guide takes a Windows machine from nothing to a running Citadel. Citadel runs in two
supported configurations, both from the same Compose file:

| | **The demonstration machine** (Part 1) | **The WSL2 distro** (Part 2) |
|---|---|---|
| Decision | [ADR-0006](./docs/adr/0006-docker-desktop-launcher-and-per-container-enforcement.md) | [ADR-0005](./docs/adr/0005-wsl2-execution-environment.md) |
| Containers run on | Docker Desktop | Docker Engine inside a dedicated WSL2 distro |
| Ollama runs | On Windows (Ollama for Windows) | Inside the same distro |
| Started with | `citadel` | `scripts/up.sh` (inside the distro) |
| Effort | Two ordinary installers | A distro, Docker Engine, the NVIDIA Container Toolkit, Ollama |
| Choose it when | You want Citadel running with one command. **This is the demonstration path.** | You want every egress-capable process inside one Linux box you administer, or Docker Desktop's licence does not fit your organisation |

**The quick way.** Open a Command Prompt in the repository folder and run one of the
commands below. In PowerShell, type `.\setup` instead of `setup`.

```
setup              the demonstration machine (Part 1)
setup wsl2         the WSL2 distro (Part 2)
setup check        check this machine and change nothing (setup wsl2 -Check for Part 2)
setup help         the options: -Yes (answer yes to every question), -NoStart, -Distro, -Port
```

`setup` checks everything below and says what it found. When something is missing it
explains what to do, and it asks before it installs, downloads or starts anything.
It ends with a summary of whatever is still wrong, and exits with status 1 if anything
failed. Run it again at any time: it only acts on what is missing. The rest of this
guide describes the same steps by hand, and what to do when one fails.

**Internet is needed only during setup**: the installers, the first image build, and
the model download. After that, Citadel runs with the network cable unplugged, which is
the point (acceptance target E).

---

## Before you start

| You need | Why |
|---|---|
| **Windows 10 22H2 or Windows 11, 64-bit** | Docker Desktop and WSL 2 need them |
| **An NVIDIA GPU with a current driver** | Ollama runs the models on it. Citadel is sized for an 8 GB card (the RTX 5060). With less, Ollama splits models between GPU and CPU. Without an NVIDIA GPU, everything runs on the CPU, slowly |
| **16 GB of RAM** (12 GB works) | Docker, Postgres, OCR and the browser share memory with Ollama |
| **About 12 GB free on the system drive** | Roughly 7 GB for the approved models; the rest for the container image, the database and the demo documents. Docker Desktop and Ollama keep their data on the system drive by default |
| **Virtualisation enabled in the firmware** | WSL 2 needs it (Intel VT-x or AMD SVM; usually on already) |
| **Administrator rights, once** | To install WSL 2 and Docker Desktop |

The models Citadel may use are the entries marked `enabled: true` in
[`registry/models.demo-local.yaml`](./registry/models.demo-local.yaml). Nothing is
downloaded until a person asks for it (`setup`, `citadel models`, or *Pull missing models*
in the UI).

---

## Part 1 — The demonstration machine

### 1. Install WSL 2

Docker Desktop runs its containers on WSL 2. In **PowerShell as Administrator**:

```powershell
wsl --install --no-distribution
```

Restart Windows. (On an older build that does not know `--no-distribution`, run
`wsl --install` instead; it also installs Ubuntu, which is harmless.) If WSL is already
installed, `wsl --update` brings it up to date.

### 2. Install Docker Desktop

Download it from docker.com/products/docker-desktop, or:

```powershell
winget install --exact --id Docker.DockerDesktop
```

During installation, keep **Use WSL 2 instead of Hyper-V** ticked. Start Docker Desktop,
accept its terms, and wait until it shows **Engine running**. Check it in a new Command
Prompt:

```
docker info
docker compose version
```

> Docker Desktop's licence is free for personal use, education and small businesses. Larger
> organisations need a paid subscription. Part 2 uses Docker Engine, which has no such
> licence requirement.

### 3. Install Ollama for Windows

Download it from ollama.com/download/windows, or:

```powershell
winget install --exact --id Ollama.Ollama
```

It starts on its own and sits in the tray, with the GPU through the ordinary Windows
driver. Check that it answers:

```
curl.exe http://127.0.0.1:11434/api/version
```

Do **not** set `OLLAMA_HOST=0.0.0.0` for Citadel. The containers reach Ollama on the
host's own loopback through Docker Desktop (`host.docker.internal:11434`). Listening
on every interface would only expose Ollama to your network.

### 4. Get the repository

Put it at a short path without spaces. This guide uses `C:\AI_WORKBENCH\SIH_2026`.
Either clone it with git, or copy the whole folder, `.git` included:

```
git clone <where the repository lives> C:\AI_WORKBENCH\SIH_2026
cd /d C:\AI_WORKBENCH\SIH_2026
```

### 5. Run setup

```
setup
```

It checks Windows, memory, disk space, the GPU, Docker Desktop, Ollama and port 8000.
It offers to download the approved models that are missing, then to build and start
Citadel. At the end it verifies the running system:

- the API is healthy;
- no external connection has been observed;
- the egress ruleset is applied;
- every approved model is installed;
- the demo documents have been read.

To do the same by hand:

```
citadel            build (the first time), start, wait for the API, open the browser
citadel models     download the approved models (once; about 7 GB)
citadel status     containers, health, and which models are missing
```

### 6. What the first start does

1. **Builds one image.** `ops/compose/Dockerfile` installs Python 3.12, Tesseract, nftables
   and the Python packages. It downloads them, plus the Postgres image. This takes a few
   minutes once; later starts take seconds.
2. **Runs `init`.** It creates the signing keys and migrates the database, then exits. This
   step is idempotent and runs on every start.
3. **Starts the stack.** Postgres and the sandbox sit on internal-only networks. The API and
   the worker each install a default-deny egress ruleset in their own network namespace
   before the application starts.
4. **Adds the demo documents.** Once the approved models are installed, the worker reads the
   demonstration corpus: sixteen documents in department folders, and a later issue of
   Policy 1. Scans are read by OCR and by the vision model, which takes a few minutes.
   Until the models are in, the worker waits on purpose: documents read without them
   would stay badly indexed.

Your data lives in Docker volumes (`pgdata`, `keys`, `citadel-data`). `citadel stop` keeps
them; only `citadel reset` deletes them.

### 7. Check that it works

Open http://127.0.0.1:8000 and sign in. There is no password at demonstration time.

| Identity | Role | Clearance | Department |
|---|---|---|---|
| R. Kulkarni (`demo-engineer-1`) | engineer | INTERNAL | process-engineering |
| S. Nair (`demo-engineer-2`) | engineer | CONFIDENTIAL | instrumentation |
| V. Rangan (`demo-approver`) | approver | CONFIDENTIAL | quality-assurance |

- [ ] **Sovereignty**: *External connections observed* reads **0**, and the egress ruleset
      shows as applied.
- [ ] **Documents**: the demo documents are *ready*.
- [ ] **Models & routing**: every approved model is installed.
- [ ] **Workbench**: type `/report on lathe L-1 covering only its condition and the vendor
      options`. A report with exactly those two sections appears, every paragraph cited.
      You can edit it and save v2, or send it back with **Revise**.
- [ ] **Task log**: the *Approval note* example produces a .docx awaiting approval. Sign
      in as V. Rangan and release it from *Approvals*.

`README.md` walks through the five acceptance targets in detail.

### 8. Go offline

Pull the models once, then disconnect the network (unplug the cable, or turn Wi-Fi off)
and run every flow again. Nothing degrades, because nothing was reaching out. The
container rules cover the containers. The cable covers the host too, including Ollama,
which runs on Windows and so is outside the container rules (ADR-0006 says so, and so does
the Sovereignty panel).

On a machine that stays connected, you can also block Ollama's own outbound traffic in
Windows Firewall, and lift the block only to pull a model. In **PowerShell as
Administrator**:

```powershell
$ollama = "$env:LOCALAPPDATA\Programs\Ollama"
New-NetFirewallRule -DisplayName "Citadel: Ollama egress" -Direction Outbound -Action Block -Program "$ollama\ollama.exe"
New-NetFirewallRule -DisplayName "Citadel: Ollama app egress" -Direction Outbound -Action Block -Program "$ollama\ollama app.exe"
# to pull a model later:  Disable-NetFirewallRule -DisplayName "Citadel: Ollama*"   (then Enable-)
```

Afterwards, `citadel status` should still report every model installed. The containers
reach Ollama inbound, so the outbound block does not stop them.

### 9. Everyday commands

| Command | What it does |
|---|---|
| `citadel` | Build if needed, start, open the browser |
| `citadel status` | Containers, API health, missing models |
| `citadel logs` | Follow every service's log (Ctrl-C to stop following) |
| `citadel models` | `ollama pull` every approved model |
| `citadel stop` | Stop. Documents, results, keys and the audit log stay |
| `citadel reset` | Stop and delete all Citadel data (type `DELETE` to confirm) |
| `setup check` | Re-check the machine and the running system; changes nothing. To read the Sovereignty panel it signs in once as R. Kulkarni, which the audit log records like any sign-in |

### 10. Settings

Every setting has a working default, and the demonstration needs none of them. Set one in
the same Command Prompt before `citadel`, for example `set CITADEL_PORT=8080`. To keep it,
run `setx CITADEL_PORT 8080` and open a new window. `.env.example` lists them all.

| Setting | Default | What it changes |
|---|---|---|
| `CITADEL_PORT` | `8000` | The browser port, published on 127.0.0.1 only |
| `CITADEL_REQUIRE_ENFORCEMENT` | `0` | `1`: the API and worker refuse to start if the egress ruleset cannot be applied |
| `CITADEL_DB_PASSWORD` | `citadel-local-only` | Postgres's password. The database never leaves its internal network |
| `CITADEL_ORG_NAME` | `Citadel demonstration plant` | The organisation printed on deliverables |

### 11. Updating

```
git pull
citadel
```

`citadel` rebuilds the image when the code changed, and `init` applies any new database
migrations. Your data stays.

---

## Part 2 — The WSL2 distro

Everything runs inside one dedicated Ubuntu distro: Docker Engine (not Docker Desktop),
Postgres, the API, the worker, the sandbox and Ollama. ADR-0005 chose this configuration.
It is still the stronger one, because every egress-capable process is inside a Linux
box you administer. One limit remains: nobody has yet written a ruleset for the distro
itself (see ADR-0006, *Revisit when*).

**Stop the demonstration stack first** (`citadel stop`) if it is running: both
configurations publish port 8000.

### 1. The Windows side

- **The NVIDIA driver, on Windows only.** CUDA reaches WSL2 through the Windows driver. Do
  **not** install a Linux NVIDIA driver inside the distro: that is the usual way GPU
  passthrough breaks. The RTX 5060 (Blackwell) needs a current driver with CUDA 12.8
  support.
- **WSL 2**, as in Part 1 step 1, then `wsl --update`.

### 2. Create the distro

```powershell
wsl --install -d Ubuntu-24.04 --name citadel
```

Ubuntu asks for a new Linux user name and password. When it then shows a Linux prompt,
type `exit`. (An older WSL without `--name` creates a distro called `Ubuntu-24.04`. Use
that name, `setup wsl2 -Distro Ubuntu-24.04`, or run `wsl --update` first.)

Keep WSL's networking in its default **NAT** mode. Ollama in the distro listens on every
interface so that the containers can reach it. In NAT mode that is still unreachable from
other machines. **Mirrored** mode would put it on your network.

### 3. Set the distro up

```
setup wsl2
```

It checks the Windows side and creates the distro if it is missing. It then runs
[`ops/wsl2/provision.sh`](./ops/wsl2/provision.sh) inside the distro; sudo asks for your
Linux password. To run that script by hand, from inside the distro:

```bash
bash /mnt/c/AI_WORKBENCH/SIH_2026/ops/wsl2/provision.sh --check   # what is missing
bash /mnt/c/AI_WORKBENCH/SIH_2026/ops/wsl2/provision.sh           # do it
```

What it does. Every step is skipped when already done, so running it again is safe:

| Step | What |
|---|---|
| systemd | Adds `[boot] systemd=true` to `/etc/wsl.conf` if needed, then asks you to restart the distro (`setup wsl2` does that itself) |
| GPU | `nvidia-smi` must see the card from inside the distro. **Blocking** (ADR-0005) |
| Packages | `ca-certificates curl git gnupg nftables` |
| Docker Engine | Installed from get.docker.com. The service is enabled, and you are added to the `docker` group. It refuses to continue if `docker` here is Docker Desktop's WSL integration: switch that off for this distro under *Settings > Resources > WSL integration*. A new distro can get it without asking, because Docker Desktop integrates with the *default* distro, and the first one you create becomes the default |
| NVIDIA Container Toolkit | Installed and configured for Docker. Then `docker run --gpus all nvidia/cuda:… nvidia-smi` must see the card: ADR-0005's second blocking check. `--no-container-gpu` skips both, if you accept running Ollama natively without that check |
| Ollama | Installed natively (ollama.com/install.sh), as a systemd service. A drop-in sets `OLLAMA_HOST=0.0.0.0:11434` so that the containers reach it through `host.docker.internal` |
| The repository | Cloned into `~/citadel` from the Windows folder, or fast-forwarded if already there. Cloning with git inside the distro keeps Linux line endings and the scripts' execute bits |

### 4. Start Citadel in the distro

`setup wsl2` offers to do this. By hand, **in a new shell** (so your `docker` group
membership applies; `wsl --terminate citadel` from Windows also works):

```bash
cd ~/citadel
scripts/up.sh            # build and start, wait for the API, report the models
scripts/up.sh models     # the approved models, into the distro's Ollama (once)
```

Open http://127.0.0.1:8000 in a browser on **Windows**. WSL forwards the port. Then check
it as in Part 1 step 7, and go offline as in step 8: disconnecting the Windows host cuts
the distro off too.

| Command (inside the distro) | What it does |
|---|---|
| `scripts/up.sh` | Build if needed, start, wait for the API |
| `scripts/up.sh status` | Containers, API health, missing models |
| `scripts/up.sh logs` | Follow every service's log |
| `scripts/up.sh models` | `ollama pull` every approved model |
| `scripts/up.sh down` | Stop (the data volumes stay) |

### 5. Keeping the distro's copy up to date

The clone's `origin` is the Windows folder. After changing the repository on Windows:

```bash
cd ~/citadel && git pull && scripts/up.sh
```

(`setup wsl2`, and `provision.sh`, fast-forward it for you too.)

---

## What `setup` checks

| Check | Passes when | If not |
|---|---|---|
| Repository | `setup` runs from the Citadel folder | Run it from there |
| Windows | 64-bit, build 19045 (10 22H2) or later | Windows Update |
| Memory / disk | 12 GB RAM or more; 12 GB free on the system drive (20 GB comfortable) | Free space, or move Docker's disk (*Settings > Resources > Advanced*) and Ollama's models (`OLLAMA_MODELS`) |
| GPU | `nvidia-smi` reports an NVIDIA card, 8 GB or more | Install the current NVIDIA driver |
| WSL 2 | `wsl --status` answers, default version 2 | Offers `wsl --install --no-distribution` (administrator rights, then a restart) |
| Docker Desktop | Installed, engine running, Compose v2 | Offers to install it (winget) or start it, and waits for the engine |
| Ollama | Installed and answering on 127.0.0.1:11434 | Offers to install it (winget) or start it |
| Port | 8000 (or `CITADEL_PORT`) free, or Citadel already on it | Stop the other program, or choose another port |
| Models | Every `enabled: true` model in the registry is in Ollama | Offers to download the missing ones |
| Citadel | API healthy, egress 0, ruleset applied, Ollama reachable from the containers, models and documents in | Offers to start it; points to the section below |
| *WSL2 only:* distro | Present; `provision.sh --check` passes | Offers to create it and set it up |

---

## Troubleshooting

**Docker Desktop never reaches "Engine running".** Enable virtualisation in the firmware
(Intel VT-x / AMD SVM), run `wsl --update`, and restart Windows. Docker Desktop's
*Troubleshoot* menu can restart or reset the engine.

**The first `citadel` fails while building.** The build downloads Python packages and the
Postgres image, so it needs the internet once. Behind a proxy, set it in Docker Desktop:
*Settings > Resources > Proxies*. The running system ignores proxies on purpose, so a
proxy never sees a prompt or a document.

**Port 8000 is in use.** `set CITADEL_PORT=8080`, then run `citadel` (or `setup`) from the
same window.

**`citadel status`: "Ollama is not answering the containers".** Start Ollama (the tray
app, or `ollama serve`) and check `curl.exe http://127.0.0.1:11434/api/version`. Leave
`OLLAMA_HOST` unset on Windows.

**Tasks fail with "no model is available".** A model is missing: run `citadel models`, then
`citadel status`. `ollama list` shows what Ollama has.

**Everything is slow.** Look at the PROCESSOR column of `ollama ps` while a task runs. If it
shows CPU, update the NVIDIA driver and close other programs that use the GPU. The model
set is sized for 8 GB of VRAM.

**The demo documents never appear.** The worker adds them only once every approved model is
installed. After `citadel models`, allow a few minutes: the scans are read by OCR and by
the vision model. `citadel logs` shows the progress.

<a id="the-egress-ruleset-is-not-applied"></a>
**The egress ruleset is not applied.** The Sovereignty panel says so in words ("could NOT be
applied"), with the reason. The usual cause is that Docker Desktop's kernel refuses
nf_tables inside a container's network namespace. The in-process fence and the telemetry
still run, and nothing is hidden. For the full boundary, use Part 2. To refuse to start
without the ruleset instead, `set CITADEL_REQUIRE_ENFORCEMENT=1`.

**Compose: "Pool overlaps with other one on this address space".** Citadel's networks use
172.30.10.0/24, 172.30.20.0/24 and 172.30.30.0/24. If one collides with a network on your
machine, change the subnets in `ops/compose/docker-compose.yml`. Change
`CITADEL_EGRESS_NETWORKS` and `CITADEL_UNTRUSTED_NETWORKS` in the same file to match.

**WSL2: `nvidia-smi` fails inside the distro.** Update the Windows driver, remove any Linux
NVIDIA driver (`sudo apt-get purge 'nvidia-driver-*'`), and run `wsl --update` and then
`wsl --shutdown`. ADR-0005 treats a failure here as blocking. Do not work around it.

**WSL2: "permission denied … docker.sock".** Your `docker` group membership applies from
the next shell: exit and reopen it, or run `wsl --terminate citadel` from Windows.

**WSL2: the containers cannot reach Ollama.** `ss -ltn | grep 11434` must show `0.0.0.0:11434`.
`provision.sh` sets this with a systemd drop-in. Check it with
`systemctl cat ollama` and apply it with `sudo systemctl restart ollama`.

**WSL2: the browser on Windows cannot open 127.0.0.1:8000.** Localhost forwarding is
switched off (`localhostForwarding=false` in `%UserProfile%\.wslconfig`), or networking is
mirrored. Use NAT mode, or open the distro's own address (`hostname -I`).

**WSL2: "bash\r: No such file or directory".** The repository was copied with Windows line
endings. Clone it inside the distro with git (`provision.sh` does), and the repository's
`.gitattributes` keeps the scripts in Linux form.

**Start over.**

- Part 1: `citadel reset` deletes every document, task, deliverable, key and the audit log.
- Part 2: `docker compose -f ops/compose/docker-compose.yml --project-name citadel down -v`
  in `~/citadel` does the same.

The models stay in Ollama either way.

---

## Next

- [`README.md`](./README.md): the demonstration, target by target, and report writing in
  the workbench.
- [`AGENTS.md`](./AGENTS.md): the rules, the module map and the current state, for anyone
  changing the code. `scripts/run.sh --fake-models` runs everything without containers or
  models, for development.
- [`docs/adr/`](./docs/adr/): why each of these choices was made.
