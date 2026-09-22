#!/usr/bin/env bash
# scripts/check.sh -- the one command that answers "is the repo in a working state?"
#
# Runs pytest, mypy --strict and ruff check, in that order, against whichever
# toolchain this machine actually has (scripts/lib/env.sh) -- the same three checks
# root AGENTS.md's "Current state" section reports the pass/fail of, run the same way
# whether this is the network-restricted dev sandbox or the real WSL2 machine. Run
# this before calling anything "done"; root AGENTS.md's "Current state" section is
# only trustworthy when it reflects this script's most recent result.
#
# Every stage runs even if an earlier one fails, so a single pass reports all of
# what's broken instead of one fix-and-rerun cycle per stage. Exit code is 0 only if
# every stage passed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/env.sh"

cd "$CITADEL_REPO_ROOT"

declare -A results
overall=0

run_stage() {
    local name="$1"
    shift
    citadel::log "== $name =="
    if "$@"; then
        results["$name"]="PASS"
    else
        results["$name"]="FAIL"
        overall=1
    fi
    echo
}

run_stage "pytest"        citadel::pytest -q
run_stage "mypy --strict" citadel::mypy
run_stage "ruff check"    citadel::ruff check .

citadel::log "== summary =="
for name in "pytest" "mypy --strict" "ruff check"; do
    citadel::log "  ${results[$name]}  $name"
done

if [ "$overall" -eq 0 ]; then
    citadel::log "repo is green."
else
    citadel::log "repo is NOT green -- see the failing stage(s) above."
fi

exit "$overall"
