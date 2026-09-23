#!/usr/bin/env bash
# scripts/lib/env.sh -- shared toolchain dispatch for scripts/check.sh and scripts/test.sh.
#
# Source this. It is not meant to be executed directly (it deliberately has no
# execute bit -- that omission is the "source me" signal), and it sets no shell
# options of its own: a library that flips -e/-u/-o pipefail behind the sourcing
# script's back is a classic footgun, so check.sh and test.sh each choose their own.
#
# Two real machines run this repo's checks, and they need different commands:
#
#   - The target WSL2 machine (ADR-0005), once `uv sync` has populated .venv from
#     every package's own pyproject.toml: plain `uv run --no-sync <tool>`.
#   - A network-restricted dev sandbox (root AGENTS.md's "Running tests/mypy/ruff in
#     a network-restricted dev sandbox" note): `uv sync` cannot reach a package
#     registry to resolve the `dev` dependency group, but pytest/mypy/ruff are
#     pre-installed as global `uv tool`s -- isolated from both the workspace venv and
#     from citadel_contracts's own runtime deps (pyjwt, cryptography) -- so they need
#     a PYTHONPATH bridge to both.
#
# This file decides which one applies by actually trying the first and falling back
# to the second -- never by guessing from hostname, OS, or an env var someone has to
# remember to set. Same scripts, either machine, no manual switch. Don't let anything
# in here leak into ops/ or the Compose files: the bridge branch is a sandbox-only
# workaround, not something the real deployment should ever need.

CITADEL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

citadel::log() { echo "[citadel] $*" >&2; }

# Cached across calls within one script invocation -- detection runs `uv run
# --no-sync`, which is fast and side-effect-free (see below) but there is still no
# reason to pay for it three times in one `check.sh` run.
_citadel_mode=""

citadel::_detect_mode() {
    [ -n "$_citadel_mode" ] && return 0

    # `uv run --no-sync` never touches the network and never resolves or installs
    # anything -- it runs against whatever .venv already exists, or creates an empty
    # one on the spot if none does, and simply fails fast with ModuleNotFoundError
    # when the venv doesn't have what was asked for. Confirmed empirically both with
    # no .venv at all and with an empty one; either way this returns in well under a
    # second with no network attempt, so it's safe to use as a probe.
    if (cd "$CITADEL_REPO_ROOT" && uv run --no-sync python -c "import pytest, mypy, ruff" >/dev/null 2>&1); then
        _citadel_mode="workspace"
        citadel::log "toolchain: workspace venv (uv run --no-sync -- .venv has pytest/mypy/ruff)"
        return 0
    fi

    if command -v pytest >/dev/null 2>&1 && command -v mypy >/dev/null 2>&1 && command -v ruff >/dev/null 2>&1; then
        _citadel_mode="sandbox-bridge"
        citadel::log "toolchain: sandbox bridge (global pytest/mypy/ruff + PYTHONPATH -- no synced .venv found)"
        return 0
    fi

    citadel::log "ERROR: no usable toolchain found."
    citadel::log "  Tried: 'uv run --no-sync python -c \"import pytest, mypy, ruff\"' -- .venv is not synced with them."
    citadel::log "  Tried: pytest/mypy/ruff on PATH -- not all three are present."
    citadel::log "  Fix: run 'uv sync' at the repo root (needs package-registry access), or"
    citadel::log "       install pytest, mypy and ruff globally (e.g. 'uv tool install <name>' for each)."
    exit 1
}

# Wherever base Python's own `import jwt` (or, failing that, `import cryptography`)
# resolves to, one directory up from the package itself -- never a hardcoded
# version-specific path like /usr/local/lib/python3.11/dist-packages, which a base
# Python upgrade would silently break. Prints nothing (not an error) if neither
# import works from plain python3; the caller decides whether that's fatal.
citadel::_bridge_dir() {
    python3 -c "import jwt, os; print(os.path.dirname(os.path.dirname(jwt.__file__)))" 2>/dev/null \
        || python3 -c "import cryptography, os; print(os.path.dirname(os.path.dirname(cryptography.__file__)))" 2>/dev/null \
        || true
}

# citadel::_nonempty_py_dirs <glob> -- every directory matching <glob> (relative to
# the repo root) that contains at least one .py file, one per output line, still
# relative to the repo root. Skips a directory with zero .py files rather than
# emitting it: mypy's own `files` config hard-errors ("There are no .py[i] files in
# directory ...") the instant a glob match is empty, and a stub package's tests/ is
# exactly that today -- this is what lets a brand-new package, or a stub's first real
# test file, get picked up automatically without that landmine.
citadel::_nonempty_py_dirs() {
    local pattern="$1" dir
    (
        cd "$CITADEL_REPO_ROOT" || exit 1
        shopt -s nullglob
        for dir in $pattern; do
            [ -d "$dir" ] || continue
            if [ -n "$(find "$dir" -name '*.py' -print -quit 2>/dev/null)" ]; then
                printf '%s\n' "$dir"
            fi
        done
    )
}

citadel::_bridge_pythonpath() {
    local bridge joined dir
    bridge="$(citadel::_bridge_dir)"
    if [ -z "$bridge" ]; then
        citadel::log "WARNING: no 'import jwt' or 'import cryptography' from plain python3 -- citadel_contracts's own runtime deps won't be on PYTHONPATH, so importing it will likely fail."
    fi
    joined="$bridge"
    while IFS= read -r dir; do
        joined="${joined:+$joined:}$CITADEL_REPO_ROOT/$dir"
    done < <(citadel::_nonempty_py_dirs "packages/*/src"; citadel::_nonempty_py_dirs "services/*/src")
    printf '%s' "$joined"
}

# citadel::run <command...> -- run one tool the right way for whichever environment
# this is: `uv run --no-sync <command>` in workspace mode, or `<command>` with the
# PYTHONPATH bridge exported in sandbox-bridge mode. Always runs from the repo root,
# regardless of the caller's own cwd.
citadel::run() {
    citadel::_detect_mode
    if [ "$_citadel_mode" = "workspace" ]; then
        ( cd "$CITADEL_REPO_ROOT" && uv run --no-sync "$@" )
    else
        ( cd "$CITADEL_REPO_ROOT" && PYTHONPATH="$(citadel::_bridge_pythonpath)" "$@" )
    fi
}

citadel::pytest() { citadel::run pytest "$@"; }
citadel::ruff() { citadel::run ruff "$@"; }

# citadel::mypy -- every packages/*/src, packages/*/tests, services/*/src and
# tests/structural that has at least one .py file, all passed to mypy in a single
# invocation (not per-package): a cross-package import -- citadel_platform importing
# citadel_contracts.classification, say -- is only checkable when mypy can see both
# packages' source at once. `--strict` itself comes from root pyproject.toml's
# [tool.mypy], not repeated here. `--ignore-missing-imports` is added only in
# sandbox-bridge mode: it papers over mypy's own isolated `uv tool` environment not
# having pydantic/jsonschema/pyjwt/cryptography installed, which is a sandbox fact,
# not a real-machine one -- a fully `uv sync`'d workspace venv has all of them for
# real, and blanket-ignoring missing imports there would mask a genuinely missing
# dependency instead of catching it. yaml's own distinct import-untyped gap is
# handled separately, by the checked-in per-module override in pyproject.toml.
#
# services/*/src joined this list when services/api (citadel_api) was first
# written, and stays here even now that it is a real uv workspace member
# (see that package's own docstring): mypy is invoked below with an explicit
# list of directories, not via uv's workspace discovery, so workspace
# membership changes what `uv sync` installs, never what this function tells
# mypy to look at. root AGENTS.md's "the repo is green" claim covers every
# package with real code in it, services included.
citadel::mypy() {
    citadel::_detect_mode
    local -a targets
    mapfile -t targets < <(
        citadel::_nonempty_py_dirs "packages/*/src"
        citadel::_nonempty_py_dirs "packages/*/tests"
        citadel::_nonempty_py_dirs "services/*/src"
        citadel::_nonempty_py_dirs "services/*/tests"
        citadel::_nonempty_py_dirs "tests/structural"
        citadel::_nonempty_py_dirs "tests/deployment"
        citadel::_nonempty_py_dirs "tests/fakes"
    )
    if [ "${#targets[@]}" -eq 0 ]; then
        citadel::log "ERROR: no .py files found under packages/*/src, packages/*/tests, services/*/src or tests/structural."
        exit 1
    fi
    if [ "$_citadel_mode" = "workspace" ]; then
        citadel::run mypy "${targets[@]}"
    else
        citadel::run mypy --ignore-missing-imports "${targets[@]}"
    fi
}
