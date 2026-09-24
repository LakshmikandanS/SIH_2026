#!/usr/bin/env bash
# scripts/run-api.sh -- kept for muscle memory: it is scripts/run.sh, which now starts
# the whole of Citadel (database, migrations, keys, worker, API and web UI).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run.sh" "$@"
