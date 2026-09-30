"""Configuration for the Execution Service process (design doc §2, §6.6).

This process is a separate OS process from the trusted workflow zone's
`app/` package (`app/config.py` is *not* shared with it -- see
`app/execution/settings.py` for the trusted-zone side of this boundary).
Style matches `app/config.py` deliberately: everything environment-driven
lives in one module, read from `os.environ` directly, so nothing here
requires the trusted zone's dependency stack.

Env vars are namespaced `CITADEL_EXECUTION_*` to keep this process's
configuration visibly distinct from the trusted zone's `CITADEL_*` settings
even when both are listed side by side in `docker/docker-compose.yml`.
"""

from __future__ import annotations

import os

# --- HTTP server -----------------------------------------------------------
HOST: str = os.environ.get("CITADEL_EXECUTION_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("CITADEL_EXECUTION_PORT", "8901"))

# --- Docker connection -------------------------------------------------------
# None => docker-py's default resolution (DOCKER_HOST env var, then the
# platform default: unix:///var/run/docker.sock on Linux, the npipe on
# Windows). Set explicitly only if the daemon lives somewhere nonstandard.
DOCKER_BASE_URL: str | None = os.environ.get("CITADEL_EXECUTION_DOCKER_URL") or None

# --- The one-shot sandbox container -----------------------------------------
# design doc §2 / §8 step 5: "python:3.12-slim".
SANDBOX_IMAGE: str = os.environ.get("CITADEL_EXECUTION_SANDBOX_IMAGE", "python:3.12-slim")

#: Memory cap, Docker's own size-string format (e.g. "256m").
SANDBOX_MEMORY_LIMIT: str = os.environ.get("CITADEL_EXECUTION_MEMORY_LIMIT", "256m")

#: CPU cap, in whole cores (fractional allowed) -- converted to nano_cpus.
SANDBOX_CPU_LIMIT: float = float(os.environ.get("CITADEL_EXECUTION_CPU_LIMIT", "1.0"))

#: Caps forkbomb-style abuse independently of memory/CPU.
SANDBOX_PIDS_LIMIT: int = int(os.environ.get("CITADEL_EXECUTION_PIDS_LIMIT", "64"))

#: Scratch space for the container's own writes (e.g. stdout buffering,
#: temp files a script creates) -- an in-memory tmpfs, not a host mount, so
#: "no host filesystem mount beyond a scratch input/output directory" (§2) is
#: satisfied with the strongest available reading: there is no host mount at
#: all.
SANDBOX_TMPFS_SIZE: str = os.environ.get("CITADEL_EXECUTION_TMPFS_SIZE", "size=64m")

#: Default time limit; killed on expiry. A caller may request a shorter one
#: per call (never a longer one) via the request body's `timeout_seconds`.
SANDBOX_TIMEOUT_SECONDS: int = int(os.environ.get("CITADEL_EXECUTION_TIMEOUT_SECONDS", "30"))

#: Hard ceiling on a per-call override, so a caller cannot ask for an
#: unbounded run.
SANDBOX_MAX_TIMEOUT_SECONDS: int = int(
    os.environ.get("CITADEL_EXECUTION_MAX_TIMEOUT_SECONDS", "120")
)
