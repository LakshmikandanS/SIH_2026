#!/usr/bin/env bash
# scripts/dev-db.sh -- start/stop/status for a disposable local Postgres.
#
# This is the prerequisite packages/platform/tests/pg_scratch.py's own skip message
# points at ("run `scripts/dev-db.sh start` and export the PGHOST/PGPORT/PGUSER it
# prints"): the integration tests and schema proofs under packages/platform/tests/
# need SOME reachable Postgres server to create their own disposable
# `citadel_test_<hex>` scratch databases against (pg_scratch.py's pg_scratch_db()).
# This script's only job is providing that server -- it deliberately does not create
# a `citadel` database or the `citadel_app` role itself; migration 0001 creates the
# role idempotently, and pg_scratch_db() creates and drops its own database per test.
#
# This is unrelated to ops/compose/'s containerized Postgres, which is what the real
# demo-local stack runs (PLAN-M0 task 11) -- that one is brought up by
# `docker compose up`, has pgvector installed, and is not what this script manages.
# This script exists so `scripts/check.sh` can run fast, without Compose, against a
# throwaway local server -- exactly the server this repo's own dev sandbox has had
# running by hand (via `su postgres -c "pg_ctl ..."`) for every integration test
# written so far. Don't let this file's assumptions leak into ops/ or the Compose
# files -- same rule as scripts/lib/env.sh's sandbox-bridge branch.
#
# Root-vs-non-root, detected the same way scripts/lib/env.sh detects its toolchain
# mode: by trying, never by guessing from hostname or a variable someone has to
# remember to set.
#   - Running as root (this dev sandbox, confirmed via `whoami`): Postgres refuses to
#     start as root ("cannot be run as root"), so every postgres/initdb/pg_ctl call
#     is delegated to the `postgres` OS user via `su`, and the data/socket
#     directories are chown'd to that user first.
#   - Running as a normal user (the expected case on the real WSL2 machine, where the
#     developer owns their own checkout): no `su` needed at all -- initdb/pg_ctl run
#     directly as that user, who already owns whatever they just `mkdir -p`'d.
#
# Data lives at .dev/pgdata, the Unix socket at .dev/pgrun, both gitignored and
# self-contained under the repo -- never /tmp, so two checkouts (or this script and
# someone's own manual `pg_ctl` experiment) can't collide on the same socket path.
# Default port 5433 (override with CITADEL_DEV_DB_PORT), specifically NOT 5432, so
# this never collides with a system-wide Postgres or a Compose-managed one on the
# default port. listen_addresses='' turns off TCP entirely -- this server is reached
# only through the socket dir it creates, so there is nothing to firewall because
# there is nothing listening on the network at all.
#
# --auth=trust and initdb --no-sync are both deliberate and both scoped to this
# disposable dev-only data directory: --auth=trust because a local, socket-only,
# throwaway database that only ever holds test fixtures has no secret worth a
# password prompt slowing down every test run; --no-sync because that data is never
# one fsync away from being irreplaceable -- `rm -rf .dev` and `start` again is
# always a valid recovery. Neither flag belongs anywhere near a real deployment.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/env.sh"

DEV_DIR="$CITADEL_REPO_ROOT/.dev"
PGDATA_DIR="$DEV_DIR/pgdata"
PGRUN_DIR="$DEV_DIR/pgrun"
LOG_FILE="$DEV_DIR/postgres.log"
DEV_DB_PORT="${CITADEL_DEV_DB_PORT:-5433}"

_is_root() { [ "$(id -u)" -eq 0 ]; }

# _pg_bin <name> -- the right way to invoke a Postgres server-side binary
# (initdb/pg_ctl) that Debian/Ubuntu deliberately does NOT put on PATH (only
# client tools like psql/createdb/pg_isready are, via /usr/bin's pg_wrapper --
# confirmed by `readlink -f /usr/bin/psql`). `pg_config --bindir` is the portable,
# version-independent way to find it, same idea as scripts/lib/env.sh's
# _bridge_dir: never a hardcoded `/usr/lib/postgresql/16/bin`, which a version
# bump on the real machine would silently break. Falls back to the bare name on
# PATH for any install that already puts it there.
_pg_bin() {
    local name="$1" bindir
    bindir="$(pg_config --bindir 2>/dev/null || true)"
    if [ -n "$bindir" ] && [ -x "$bindir/$name" ]; then
        printf '%s' "$bindir/$name"
    else
        printf '%s' "$name"
    fi
}

_require_binary() {
    local name="$1"
    if ! command -v "$(_pg_bin "$name")" >/dev/null 2>&1; then
        citadel::log "ERROR: '$name' not found (tried 'pg_config --bindir' and PATH)."
        citadel::log "  Install a local Postgres server (e.g. 'apt install postgresql') to use dev-db.sh."
        exit 1
    fi
}

# _as_pg_owner <command...> -- run a command as the postgres data directory's
# owner: `su postgres -c` when we are root, or directly when we are already some
# non-root user. %q-quotes each argument before joining for `su -c`, which takes
# one shell string, so paths are never re-split or re-interpreted by that inner
# shell.
_as_pg_owner() {
    if _is_root; then
        local cmd="" arg quoted
        for arg in "$@"; do
            printf -v quoted '%q' "$arg"
            cmd="$cmd $quoted"
        done
        su postgres -c "$cmd"
    else
        "$@"
    fi
}

_status_quiet() {
    pg_isready -h "$PGRUN_DIR" -p "$DEV_DB_PORT" -q >/dev/null 2>&1
}

_print_export_lines() {
    # Deliberately the only thing this script writes to stdout -- citadel::log
    # writes to stderr -- so `eval "$(scripts/dev-db.sh start)"` works too, though
    # the documented path is copy-pasting these three lines.
    echo "export PGHOST=$PGRUN_DIR"
    echo "export PGPORT=$DEV_DB_PORT"
    echo "export PGUSER=postgres"
}

cmd_start() {
    _require_binary initdb
    _require_binary pg_ctl
    _require_binary pg_isready

    if _status_quiet; then
        citadel::log "already running"
        _print_export_lines
        return 0
    fi

    mkdir -p "$PGDATA_DIR" "$PGRUN_DIR"
    if _is_root; then
        chown -R postgres:postgres "$DEV_DIR"
    fi

    if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
        citadel::log "initializing data directory: $PGDATA_DIR"
        if ! _as_pg_owner "$(_pg_bin initdb)" -D "$PGDATA_DIR" -U postgres --auth=trust --no-sync >/dev/null; then
            citadel::log "ERROR: initdb failed."
            return 1
        fi
    fi

    citadel::log "starting postgres: port $DEV_DB_PORT, socket dir $PGRUN_DIR, log $LOG_FILE"
    if ! _as_pg_owner "$(_pg_bin pg_ctl)" -D "$PGDATA_DIR" -l "$LOG_FILE" -w -t 30 \
            -o "-p $DEV_DB_PORT -k $PGRUN_DIR -c listen_addresses=''" start; then
        citadel::log "ERROR: postgres failed to start -- see $LOG_FILE"
        return 1
    fi

    citadel::log "started. Export these (or eval this script's stdout) before running the tests:"
    _print_export_lines
}

cmd_stop() {
    _require_binary pg_ctl
    _require_binary pg_isready

    if ! _status_quiet; then
        citadel::log "not running"
        return 0
    fi

    _as_pg_owner "$(_pg_bin pg_ctl)" -D "$PGDATA_DIR" -m fast stop
    citadel::log "stopped."
}

cmd_status() {
    _require_binary pg_isready

    if _status_quiet; then
        citadel::log "running -- port $DEV_DB_PORT, socket dir $PGRUN_DIR"
        _print_export_lines
        return 0
    else
        citadel::log "not running"
        return 1
    fi
}

case "${1:-}" in
    start)  cmd_start;  exit $? ;;
    stop)   cmd_stop;   exit $? ;;
    status) cmd_status; exit $? ;;
    *)
        echo "usage: $0 {start|stop|status}" >&2
        exit 2
        ;;
esac
