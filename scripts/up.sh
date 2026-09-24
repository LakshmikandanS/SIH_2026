#!/usr/bin/env bash
# scripts/up.sh -- the containerised stack on Linux or WSL2: the same Compose file
# citadel.cmd drives on Windows (docs/adr/0006). This is also how ADR-0005's
# configuration runs -- Docker Engine and Ollama inside a dedicated WSL2 distro.
#
#   scripts/up.sh            build and start, wait for the API, report the models
#   scripts/up.sh models     ollama pull every approved model (registry/models.<profile>.yaml)
#   scripts/up.sh status     containers, API health, models
#   scripts/up.sh logs       follow every service's log
#   scripts/up.sh down       stop (named volumes -- documents, results, keys -- stay)
#
# Ollama must answer where the containers look for it. On Docker Desktop that is the
# host's own 127.0.0.1:11434 via host.docker.internal. On plain Linux Docker, Ollama
# must listen beyond loopback (OLLAMA_HOST=0.0.0.0) for host.docker.internal to reach it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/ops/compose/docker-compose.yml" --project-name citadel)
PORT="${CITADEL_PORT:-8000}"

report_models() {
    local missing rc=0
    missing="$("${COMPOSE[@]}" exec -T api python -m citadel_worker missing-models 2>/dev/null)" || rc=$?
    if [ "$rc" = 2 ]; then
        echo "[citadel] the inference runtime is not answering the containers -- is Ollama running (OLLAMA_HOST=0.0.0.0 on Linux)?"
    elif [ "$rc" = 0 ] && [ -n "$missing" ]; then
        echo "[citadel] models not pulled yet: $(echo $missing) -- run: scripts/up.sh models"
        echo "[citadel] (the demo documents are added as soon as they are in)"
    elif [ "$rc" = 0 ]; then
        echo "[citadel] all approved models are installed"
    fi
}

case "${1:-up}" in
    up)
        "${COMPOSE[@]}" up -d --build
        printf '[citadel] waiting for the API'
        for _ in $(seq 1 150); do
            if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
                printf '\n[citadel] Citadel is running at http://127.0.0.1:%s\n' "$PORT"
                report_models
                exit 0
            fi
            printf '.'
            sleep 2
        done
        printf '\n[citadel] the API did not come up; see: scripts/up.sh logs\n'
        exit 1
        ;;
    models)
        command -v ollama >/dev/null 2>&1 || { echo "[citadel] no ollama command here"; exit 1; }
        tags="$("${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python init -c \
            "from pathlib import Path; from citadel_platform.registry import load_registry; r = load_registry('demo-local', Path('/app/registry')); print(' '.join(m.tag for m in r.models if m.enabled))")"
        for tag in $tags; do
            echo "[citadel] ollama pull $tag"
            ollama pull "$tag"
        done
        echo "[citadel] models ready"
        ;;
    down) "${COMPOSE[@]}" down ;;
    logs) "${COMPOSE[@]}" logs -f --tail 200 ;;
    ps|status)
        "${COMPOSE[@]}" ps
        if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
            echo "[citadel] API healthy at http://127.0.0.1:$PORT"
            report_models
        else
            echo "[citadel] API not answering at http://127.0.0.1:$PORT"
        fi
        ;;
    *) echo "usage: scripts/up.sh [up|models|status|logs|down]"; exit 2 ;;
esac
