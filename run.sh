#!/usr/bin/env bash
# Start Citadel — both processes, with prerequisites checked first.
#
# Citadel runs as two OS processes on purpose (design doc §2):
#   * execution_service — the ONLY holder of a Docker socket
#   * app.main          — the trusted workflow zone and the /ui console
#
# Usage:
#   ./run.sh              start both, wait for healthy, print where to go
#   ./run.sh --check      verify prerequisites and exit
#   ./run.sh --stop       stop both
#   ./run.sh --no-browser don't open a browser

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/var/logs"
APP_PORT="${CITADEL_SERVER_PORT:-8420}"
EXEC_PORT="${CITADEL_EXECUTION_PORT:-8901}"

say()  { echo "  $*"; }
ok()   { printf "  \033[32m[ ok ]\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m[warn]\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m[fail]\033[0m %s\n" "$*"; }
head_() { printf "\n\033[36m%s\033[0m\n" "$*"; }

stop_port() {
  local port="$1" label="$2" pids
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs -r kill -9 2>/dev/null || true
    ok "stopped $label (port $port)"
  else
    say "$label was not running"
  fi
}

wait_healthy() {
  local url="$1" seconds="$2" i=0
  while [ "$i" -lt "$seconds" ]; do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

MODE=start
OPEN_BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --stop)       MODE=stop ;;
    --check)      MODE=check ;;
    --no-browser) OPEN_BROWSER=0 ;;
    *) bad "unknown option: $arg"; exit 2 ;;
  esac
done

if [ "$MODE" = stop ]; then
  head_ "Stopping Citadel"
  stop_port "$APP_PORT" 'trusted zone'
  stop_port "$EXEC_PORT" 'execution service'
  echo
  exit 0
fi

echo
echo "  CITADEL — sovereign on-premise agentic AI workbench"
echo "  ---------------------------------------------------"

head_ "Checking prerequisites"
FATAL=()

if [ ! -x "$PY" ]; then
  warn "no virtualenv at .venv — creating one"
  python3 -m venv "$ROOT/.venv"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r "$ROOT/requirements-dev.txt"
  ok "virtualenv created and dependencies installed"
else
  ok "python $("$PY" --version 2>&1)"
  if ! "$PY" -c "import fastapi,uvicorn,sqlalchemy,pydantic,jwt,bcrypt,httpx,docker,typer" 2>/dev/null; then
    warn "dependencies missing or out of date — installing"
    "$PY" -m pip install --quiet -r "$ROOT/requirements-dev.txt"
  fi
  ok "dependencies present"
fi

if ! command -v docker >/dev/null 2>&1; then
  FATAL+=("Docker CLI not found. Install Docker: https://docs.docker.com/engine/install/")
elif ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  FATAL+=("Docker is installed but the daemon is not running. Start it and re-run.")
else
  ok "docker daemon $(docker version --format '{{.Server.Version}}')"
fi

if TAGS="$(curl -fsS -m 5 http://localhost:11434/api/tags 2>/dev/null)"; then
  ok "ollama serving"
  for m in hermes3 nomic-embed-text; do
    if echo "$TAGS" | grep -q "\"$m"; then ok "model $m"
    else FATAL+=("model '$m' not pulled. Run:  ollama pull $m"); fi
  done
else
  FATAL+=("Ollama is not responding on localhost:11434. See https://ollama.com/, then: ollama pull hermes3 && ollama pull nomic-embed-text")
fi

if [ "${#FATAL[@]}" -gt 0 ]; then
  head_ "Cannot start"
  for f in "${FATAL[@]}"; do bad "$f"; done
  echo
  exit 1
fi

if [ "$MODE" = check ]; then head_ "All prerequisites satisfied."; echo; exit 0; fi

head_ "Starting"
mkdir -p "$LOG_DIR"
stop_port "$EXEC_PORT" 'execution service'
stop_port "$APP_PORT" 'trusted zone'
sleep 1

# Execution zone first: python.execute fails fast and confusingly if nothing
# is listening for it.
( cd "$ROOT" && nohup "$PY" -m execution_service \
    >"$LOG_DIR/execution_service.log" 2>"$LOG_DIR/execution_service.err" & )

if wait_healthy "http://127.0.0.1:$EXEC_PORT/health" 45; then
  ok "execution service   http://127.0.0.1:$EXEC_PORT  (the only Docker-socket holder)"
else
  bad "execution service did not become healthy — see var/logs/execution_service.err"
  tail -n 15 "$LOG_DIR/execution_service.err" 2>/dev/null || true
  exit 1
fi

# Trusted zone. First start seeds demo personas and ingests the corpus, so it
# can take a few seconds longer.
( cd "$ROOT" && nohup "$PY" -m app.main \
    >"$LOG_DIR/app.log" 2>"$LOG_DIR/app.err" & )

if wait_healthy "http://127.0.0.1:$APP_PORT/ui" 90; then
  ok "trusted zone        http://127.0.0.1:$APP_PORT"
else
  bad "trusted zone did not become healthy — see var/logs/app.err"
  tail -n 15 "$LOG_DIR/app.err" 2>/dev/null || true
  exit 1
fi

head_ "Citadel is running"
cat <<EOF

  Console      http://127.0.0.1:$APP_PORT/ui

  Sign in as   j.rao / engineer-pw    submits tasks
               a.singh / approver-pw  releases artifacts
               s.mehta / admin-pw     kill-switch

  Or drive it from a shell:
    .venv/bin/python -m cli login
    .venv/bin/python -m cli task "..." --classification CONFIDENTIAL

  Logs   var/logs/      Stop   ./run.sh --stop

EOF

if [ "$OPEN_BROWSER" = 1 ]; then
  ( command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:$APP_PORT/ui" >/dev/null 2>&1 ) || \
  ( command -v open     >/dev/null && open     "http://127.0.0.1:$APP_PORT/ui" >/dev/null 2>&1 ) || true
fi
