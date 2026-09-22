#!/usr/bin/env bash
# scripts/run-api.sh -- bring up the dev database, make sure the persistent
# `citadel_demo` database exists and is migrated, then start citadel_api. The web
# UI (web/src/) is served from the same process -- see services/api/src/citadel_api/
# app.py's module docstring -- so this one command is the whole checkpoint.
#
# Idempotent and safe to run repeatedly, the same way scripts/dev-db.sh and
# scripts/lib/env.sh already are: this file does not re-decide root-vs-non-root or
# workspace-vs-sandbox-bridge itself, it just calls the scripts that already do.
#
# CITADEL_PROFILE defaults to demo-local (ADR-0004: M0 targets demo-local only,
# matching .env.example's own default). PGDATABASE defaults to citadel_demo, a
# persistent database distinct from pg_scratch_db()'s throwaway per-test ones
# (packages/platform/tests/pg_scratch.py) -- this one keeps its seeded demo
# identities and its audit log across restarts, which is the point of a checkpoint.
#
# scripts/lib/bootstrap_demo_db.py does "create the database, apply every
# migration this demo needs" -- see that file's own docstring for why it is not
# simply `python -m citadel_platform.migrations up`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/env.sh"

citadel::log "ensuring the dev database is running (scripts/dev-db.sh start)"
eval "$("$SCRIPT_DIR/dev-db.sh" start 2>/dev/null | grep '^export')"
if [ -z "${PGHOST:-}" ]; then
    citadel::log "ERROR: could not start the dev database -- run 'scripts/dev-db.sh start' directly to see why."
    exit 1
fi

export PGDATABASE="${PGDATABASE:-citadel_demo}"
export CITADEL_PROFILE="${CITADEL_PROFILE:-demo-local}"

citadel::log "database: $PGDATABASE on $PGHOST:$PGPORT (user $PGUSER)"
if ! citadel::run python3 "$SCRIPT_DIR/lib/bootstrap_demo_db.py"; then
    citadel::log "ERROR: database bootstrap failed -- see the output above."
    exit 1
fi

HOST="${CITADEL_API_HOST:-127.0.0.1}"
PORT="${CITADEL_API_PORT:-8000}"
citadel::log "profile: $CITADEL_PROFILE"
citadel::log "starting citadel_api on http://$HOST:$PORT (the web UI is served from the same address)"
citadel::run python3 -m citadel_api
