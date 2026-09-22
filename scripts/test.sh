#!/usr/bin/env bash
# scripts/test.sh -- fast pytest-only loop for local iteration.
#
# Forwards every argument to pytest (e.g. `scripts/test.sh packages/contracts -k
# receipts -x`), using the same toolchain-detection as scripts/check.sh, just without
# paying for mypy and ruff on every edit-run cycle. scripts/check.sh -- not this
# script -- is what decides a change is actually done.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/env.sh"

citadel::pytest "$@"
