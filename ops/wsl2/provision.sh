#!/usr/bin/env bash
# ops/wsl2/provision.sh -- prepare a WSL2 distro to run Citadel the ADR-0005 way.
#
# Installs, inside THIS distro and nowhere else:
#   * Docker Engine (not Docker Desktop's WSL integration),
#   * nftables,
#   * the NVIDIA Container Toolkit (ADR-0005's GPU check runs a CUDA container),
#   * Ollama, natively, with the GPU through the Windows driver, listening where the
#     containers reach it (host.docker.internal:11434),
# then clones the repository into ~/citadel, where scripts/up.sh runs the stack.
#
# Run it from inside the distro -- `setup wsl2` on Windows does this for you:
#
#   bash /mnt/c/AI_WORKBENCH/SIH_2026/ops/wsl2/provision.sh
#   bash .../provision.sh --check              report only; change nothing
#   bash .../provision.sh --repo <path|url>    clone from here (default: this repository)
#   bash .../provision.sh --dir <path>         clone into here (default: ~/citadel)
#   bash .../provision.sh --no-container-gpu   skip the NVIDIA Container Toolkit and its test
#
# Every step is skipped when it is already done, so running it again is safe. sudo asks
# for your Linux password where a step needs it. Setup downloads from docker.com,
# ollama.com, nvidia.github.io and Docker Hub; once it is done, Citadel itself needs none
# of them.
#
# Exit status: 0 ready; 1 something needs fixing (the output says what);
#              3 restart the distro (from Windows: wsl --terminate <distro>), run again.

set -uo pipefail

CHECK_ONLY=0
CONTAINER_GPU=1
DIR="${HOME}/citadel"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="$HERE"
CUDA_TEST_IMAGE="nvidia/cuda:12.8.0-base-ubuntu24.04"

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --no-container-gpu) CONTAINER_GPU=0 ;;
        --repo) REPO="${2:?--repo needs a path or URL}"; shift ;;
        --dir) DIR="${2:?--dir needs a path}"; shift ;;
        -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

FAILED=0
NEW_GROUP=0
say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '  [OK]   %s\n' "$*"; }
info() { printf '  [INFO] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILED=1; }
todo() { printf '  [TODO] %s\n' "$*"; FAILED=1; }   # --check: would be done by a real run

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
as_root() { if [ -n "$SUDO" ]; then sudo "$@"; else "$@"; fi; }

# ---------------------------------------------------------------------------- the distro
say "This distro"
if grep -qi microsoft /proc/version 2>/dev/null; then
    ok "running inside WSL ($(uname -r))"
else
    warn "this does not look like WSL2; the steps still suit a plain Ubuntu machine, but the GPU notes are WSL-specific"
fi
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ok "${PRETTY_NAME:-$ID}" ;;
        *) bad "needs Ubuntu or Debian (apt); this is ${PRETTY_NAME:-unknown}"; exit 1 ;;
    esac
fi
if [ "$(id -u)" -eq 0 ]; then
    warn "running as root: the clone and the docker group will belong to root; run it as your own user instead"
fi
if [ "$CHECK_ONLY" = 0 ] && [ -n "$SUDO" ]; then
    echo "  sudo may ask for your Linux password (once, for the whole run)."
    sudo -v || { bad "sudo is needed to install packages"; exit 1; }
fi

if [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]; then
    ok "systemd is running (Docker and Ollama run as services)"
else
    if [ "$CHECK_ONLY" = 1 ]; then
        todo "systemd is not running: /etc/wsl.conf needs [boot] systemd=true"
    else
        if [ -f /etc/wsl.conf ] && grep -qE '^\s*systemd\s*=\s*true' /etc/wsl.conf; then
            info "systemd=true is already in /etc/wsl.conf; the distro has not been restarted since"
        elif [ -f /etc/wsl.conf ] && grep -qE '^\s*systemd\s*=' /etc/wsl.conf; then
            as_root sed -i -E 's/^\s*systemd\s*=.*/systemd=true/' /etc/wsl.conf
        elif [ -f /etc/wsl.conf ] && grep -qE '^\s*\[boot\]' /etc/wsl.conf; then
            as_root sed -i '/^\s*\[boot\]/a systemd=true' /etc/wsl.conf
        else
            # A leading newline, in case the file does not end with one.
            printf '\n[boot]\nsystemd=true\n' | as_root tee -a /etc/wsl.conf >/dev/null
        fi
        ok "systemd enabled in /etc/wsl.conf"
        echo
        echo "  Restart this distro so systemd starts, then run this script again:"
        echo "    from Windows:  wsl --terminate ${WSL_DISTRO_NAME:-<this-distro>}"
        exit 3
    fi
fi

# ---------------------------------------------------------------------------- the GPU
say "GPU (through the Windows driver)"
NVSMI="$(command -v nvidia-smi 2>/dev/null || true)"
[ -z "$NVSMI" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ] && NVSMI=/usr/lib/wsl/lib/nvidia-smi
if [ -n "$NVSMI" ] && "$NVSMI" -L >/dev/null 2>&1; then
    ok "visible here: $("$NVSMI" --query-gpu=name,driver_version,memory.total --format=csv,noheader | head -1)"
else
    bad "no GPU visible inside the distro (nvidia-smi). Update the WINDOWS NVIDIA driver; do not install a Linux driver here. ADR-0005 treats this as blocking."
fi
if dpkg -l 2>/dev/null | grep -qE '^ii\s+nvidia-driver-'; then
    warn "a Linux NVIDIA driver package is installed -- the usual way GPU passthrough breaks in WSL2. Remove it: sudo apt-get purge 'nvidia-driver-*'"
fi

# ---------------------------------------------------------------------------- packages
say "Base packages"
missing=()
for pkg in ca-certificates curl git gnupg nftables; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ ${#missing[@]} -eq 0 ]; then
    ok "ca-certificates, curl, git, gnupg, nftables"
elif [ "$CHECK_ONLY" = 1 ]; then
    todo "to install: ${missing[*]}"
else
    if as_root apt-get update -qq && as_root apt-get install -y -qq "${missing[@]}"; then
        ok "installed: ${missing[*]}"
    else
        bad "apt-get could not install: ${missing[*]}"
    fi
fi

# ---------------------------------------------------------------------------- Docker Engine
say "Docker Engine"
docker_path="$(command -v docker 2>/dev/null || true)"
# A docker on the Windows PATH (appended through interop) is not this distro's.
case "$docker_path" in /mnt/[a-z]/*) docker_path="" ;; esac
desktop=0
if [ -n "$docker_path" ] && readlink -f "$docker_path" | grep -q docker-desktop; then desktop=1; fi
if [ -n "$docker_path" ] && docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -q "Docker Desktop"; then desktop=1; fi
if [ "$desktop" = 1 ]; then
    bad "docker here is Docker Desktop's WSL integration. ADR-0005 needs Docker Engine in this distro: in Docker Desktop, Settings > Resources > WSL integration, switch it off for this distro, then run this again."
    exit 1
fi
if command -v dockerd >/dev/null 2>&1; then
    ok "installed: $(dockerd --version 2>/dev/null)"
elif [ "$CHECK_ONLY" = 1 ]; then
    todo "Docker Engine is not installed"
else
    echo "  installing Docker Engine (get.docker.com; it pauses 20 s to suggest Docker Desktop -- that is expected)"
    if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && as_root sh /tmp/get-docker.sh; then
        ok "Docker Engine installed"
    else
        bad "the Docker Engine install failed (see above)"
    fi
    rm -f /tmp/get-docker.sh
fi
if command -v dockerd >/dev/null 2>&1; then
    if systemctl is-active --quiet docker 2>/dev/null; then
        ok "the docker service is running"
    elif [ "$CHECK_ONLY" = 1 ]; then
        todo "the docker service is not running"
    else
        if as_root systemctl enable --now docker >/dev/null 2>&1; then
            ok "the docker service is enabled and running"
        else
            bad "the docker service does not start: sudo journalctl -u docker"
        fi
    fi
    # The plugin's own version: no daemon, so no sudo (and no prompt under --check).
    if docker compose version >/dev/null 2>&1; then
        ok "docker compose: $(docker compose version --short 2>/dev/null)"
    else
        bad "the docker compose plugin is missing: sudo apt-get install docker-compose-plugin"
    fi
    if [ "$(id -u)" -eq 0 ]; then
        ok "root uses docker directly"
    elif id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
        ok "$(id -un) is in the docker group"
    elif [ "$CHECK_ONLY" = 1 ]; then
        todo "$(id -un) is not in the docker group"
    else
        if as_root usermod -aG docker "$(id -un)"; then
            ok "added $(id -un) to the docker group (applies from the next shell)"
            NEW_GROUP=1
        else
            bad "could not add $(id -un) to the docker group"
        fi
    fi
fi

# ---------------------------------------------------------------------------- NVIDIA Container Toolkit
if [ "$CONTAINER_GPU" = 1 ]; then
    say "NVIDIA Container Toolkit (ADR-0005's GPU check)"
    if command -v nvidia-ctk >/dev/null 2>&1; then
        ok "installed: $(nvidia-ctk --version 2>/dev/null | head -1)"
    elif [ "$CHECK_ONLY" = 1 ]; then
        todo "the NVIDIA Container Toolkit is not installed"
    else
        keyring=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        if curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | as_root gpg --dearmor --yes -o "$keyring" \
            && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                | sed "s#deb https://#deb [signed-by=$keyring] https://#g" \
                | as_root tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null \
            && as_root apt-get update -qq && as_root apt-get install -y -qq nvidia-container-toolkit; then
            ok "installed"
        else
            bad "the NVIDIA Container Toolkit install failed (see above)"
        fi
    fi
    if command -v nvidia-ctk >/dev/null 2>&1 && command -v dockerd >/dev/null 2>&1; then
        if [ "$CHECK_ONLY" = 0 ] && ! grep -qs nvidia /etc/docker/daemon.json; then
            if as_root nvidia-ctk runtime configure --runtime=docker >/dev/null && as_root systemctl restart docker; then
                ok "docker configured for the GPU"
            else
                bad "nvidia-ctk could not configure docker"
            fi
        fi
        if [ "$CHECK_ONLY" = 0 ]; then
            echo "  GPU inside a container (downloads a small CUDA image once):"
            if as_root docker run --rm --gpus all "$CUDA_TEST_IMAGE" nvidia-smi -L; then
                ok "a container sees the GPU"
            else
                bad "a container cannot see the GPU. ADR-0005 treats this as blocking; see SETUP.md, WSL2 troubleshooting."
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------- Ollama
say "Ollama (natively in this distro)"
if command -v ollama >/dev/null 2>&1; then
    ok "installed: $(ollama --version 2>/dev/null | tail -1)"
elif [ "$CHECK_ONLY" = 1 ]; then
    todo "Ollama is not installed"
else
    if curl -fsSL https://ollama.com/install.sh -o /tmp/ollama-install.sh && sh /tmp/ollama-install.sh; then
        ok "Ollama installed"
    else
        bad "the Ollama install failed (see above)"
    fi
    rm -f /tmp/ollama-install.sh
fi
dropin=/etc/systemd/system/ollama.service.d/citadel.conf
if command -v ollama >/dev/null 2>&1; then
    # The containers reach Ollama through the Docker bridge's gateway (host.docker.internal
    # -> host-gateway), so it must listen beyond loopback. In WSL2's default NAT mode that
    # is still unreachable from any other machine; see SETUP.md for mirrored networking.
    if grep -qs 'OLLAMA_HOST=0.0.0.0' "$dropin"; then
        ok "set to listen where the containers reach it ($dropin)"
    elif [ "$CHECK_ONLY" = 1 ]; then
        todo "Ollama listens on loopback only; the containers cannot reach it (needs $dropin)"
    else
        as_root mkdir -p "$(dirname "$dropin")"
        printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' | as_root tee "$dropin" >/dev/null
        as_root systemctl daemon-reload
        as_root systemctl enable ollama >/dev/null 2>&1
        as_root systemctl restart ollama
        ok "Ollama now listens on 0.0.0.0:11434 ($dropin)"
    fi
    answering=0
    for _ in $(seq 1 20); do
        if curl -fs http://127.0.0.1:11434/api/version >/dev/null 2>&1; then answering=1; break; fi
        [ "$CHECK_ONLY" = 1 ] && break
        sleep 1
    done
    if [ "$answering" = 1 ]; then
        ok "answering: $(curl -fs http://127.0.0.1:11434/api/version)"
    else
        bad "Ollama is not answering on 127.0.0.1:11434: sudo systemctl status ollama"
    fi
fi

# ---------------------------------------------------------------------------- the repository
say "The repository in ${DIR}"
if [ -f "${DIR}/scripts/up.sh" ] && git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ok "present at $(git -C "$DIR" log -1 --format='%h %s' 2>/dev/null)"
    origin="$(git -C "$DIR" remote get-url origin 2>/dev/null || true)"
    if [ "$CHECK_ONLY" = 0 ] && [ -n "$origin" ]; then
        if git -c safe.directory="$origin" -c safe.directory="$origin/.git" -C "$DIR" pull --ff-only -q; then
            ok "up to date with $origin ($(git -C "$DIR" log -1 --format='%h'))"
        else
            warn "could not fast-forward from $origin (local changes in $DIR?)"
        fi
    fi
elif [ -e "$DIR" ]; then
    bad "$DIR exists but is not a Citadel clone; move it aside or pass --dir"
elif [ "$CHECK_ONLY" = 1 ]; then
    todo "not cloned yet (from $REPO)"
else
    if git -c safe.directory="$REPO" -c safe.directory="$REPO/.git" clone -q "$REPO" "$DIR"; then
        ok "cloned from $REPO ($(git -C "$DIR" log -1 --format='%h %s'))"
    else
        bad "could not clone $REPO into $DIR"
    fi
fi
if [ -f "${DIR}/scripts/up.sh" ] && [ ! -x "${DIR}/scripts/up.sh" ]; then
    warn "scripts/up.sh is not executable in the clone; run it as: bash scripts/up.sh"
fi

# ---------------------------------------------------------------------------- summary
say "Summary"
if [ "$FAILED" = 1 ] && [ "$CHECK_ONLY" = 1 ]; then
    echo "  Not ready yet. Run without --check to do the steps marked TODO."
    exit 1
elif [ "$FAILED" = 1 ]; then
    echo "  Something above needs fixing first (FAIL lines); then run this again."
    exit 1
fi
echo "  This distro is ready for Citadel."
if [ "$NEW_GROUP" = 1 ]; then
    echo "  Your docker group membership applies from the next shell: exit and reopen it"
    echo "  (or from Windows: wsl --terminate ${WSL_DISTRO_NAME:-<this-distro>}) before the commands below."
fi
cat <<EOF

  Next, in this distro:
    cd ${DIR}
    scripts/up.sh            # build and start, wait for the API
    scripts/up.sh models     # the approved models, once (several GB)
  Then open http://127.0.0.1:8000 in a browser on Windows.
EOF
exit 0
