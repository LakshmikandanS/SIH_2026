#!/usr/bin/env bash
# scripts/run.sh -- run the whole of Citadel on this machine without containers:
# the dev Postgres (scripts/dev-db.sh), migrations, keys, the worker (agent loop +
# ingestion; it adds the demo corpus once the approved models are installed) and the
# API with the web UI.
#
#   scripts/run.sh                 # models from the Ollama at $CITADEL_INFERENCE_ENDPOINT
#                                  #   (default http://127.0.0.1:11434)
#   scripts/run.sh --fake-models   # a scripted stand-in runtime instead (tests/fakes) --
#                                  #   for trying the flows on a machine with no GPU; the UI
#                                  #   shows the fake's endpoint, so nothing pretends otherwise
#
# The Windows launcher (citadel.cmd, Docker Compose) is the demonstration path; this
# script is the developer path on Linux/WSL2, and the one scripts/check.sh's
# environment already has. Ctrl-C stops everything it started.
#
# Needs: PostgreSQL 16 with the pgvector extension, Tesseract, and the Python
# dependencies (uv sync, or the sandbox bridge scripts/lib/env.sh detects).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/env.sh"
cd "$CITADEL_REPO_ROOT"

FAKE=0
for arg in "$@"; do
    case "$arg" in
        --fake-models) FAKE=1 ;;
        -h|--help) sed -n 2,17p "$0"; exit 0 ;;
        *) citadel::log "unknown option: $arg"; exit 2 ;;
    esac
done

mkdir -p .dev/logs
citadel::log "starting the dev database (scripts/dev-db.sh)"
eval "$("$SCRIPT_DIR/dev-db.sh" start 2>/dev/null | grep '^export')"
if [ -z "${PGHOST:-}" ]; then
    citadel::log "ERROR: the dev database did not start -- run scripts/dev-db.sh start to see why."
    exit 1
fi
export PGDATABASE="${PGDATABASE:-citadel_demo}"
export CITADEL_PROFILE="${CITADEL_PROFILE:-demo-local}"
export CITADEL_KEYS_DIR="${CITADEL_KEYS_DIR:-$CITADEL_REPO_ROOT/.dev/keys}"
export CITADEL_DATA_DIR="${CITADEL_DATA_DIR:-$CITADEL_REPO_ROOT/.citadel-data}"
export CITADEL_ORG_NAME="${CITADEL_ORG_NAME:-Citadel demonstration plant}"

citadel::log "database $PGDATABASE: applying migrations"
if ! citadel::run python3 -m citadel_platform.migrations up --create-database --wait 30; then
    citadel::log "ERROR: migrations failed. Citadel needs the pgvector extension in this Postgres"
    citadel::log "       (Debian/Ubuntu: apt install postgresql-16-pgvector) -- or use citadel.cmd / Docker Compose."
    exit 1
fi
citadel::run python3 -m citadel_platform.keyring init --dir "$CITADEL_KEYS_DIR" >/dev/null

pids=()
cleanup() {
    citadel::log "stopping ${#pids[@]} background process(es)"
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

if [ "$FAKE" = 1 ]; then
    export CITADEL_INFERENCE_ENDPOINT="http://127.0.0.1:${CITADEL_FAKE_PORT:-11435}"
    citadel::log "scripted stand-in model runtime on $CITADEL_INFERENCE_ENDPOINT (tests/fakes)"
    citadel::run python3 tests/fakes/fake_ollama.py --scripted --port "${CITADEL_FAKE_PORT:-11435}" \
        > .dev/logs/fake-models.log 2>&1 &
    pids+=($!)
    sleep 1
else
    export CITADEL_INFERENCE_ENDPOINT="${CITADEL_INFERENCE_ENDPOINT:-http://127.0.0.1:11434}"
    citadel::log "models from Ollama at $CITADEL_INFERENCE_ENDPOINT"
fi

citadel::log "starting the worker (log: .dev/logs/worker.log)"
CITADEL_SEED_CORPUS="${CITADEL_SEED_CORPUS:-1}" citadel::run python3 -m citadel_worker > .dev/logs/worker.log 2>&1 &
pids+=($!)

HOST="${CITADEL_API_HOST:-127.0.0.1}"
PORT="${CITADEL_API_PORT:-8000}"
citadel::log "Citadel is at http://$HOST:$PORT  (Ctrl-C to stop)"
CITADEL_API_HOST="$HOST" CITADEL_API_PORT="$PORT" citadel::run python3 -m citadel_api
