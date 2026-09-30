"""The Execution Service -- the isolated execution zone (design doc §2, §6.6).

    "┌────────────────────── ISOLATED EXECUTION ZONE (separate) ─────────────────────┐
    │                                                                                 │
    │  Execution Service (holds the only Docker socket in the system)               │
    │         │                                                                     │
    │         ▼                                                                     │
    │  One-shot `python.execute` container: no Docker socket, no host mount,       │
    │  no network route to Postgres/vector store/internet, resource + time limits, │
    │  destroyed immediately after the call returns."

This package is deliberately **not** a subpackage of `app/` -- it is a
separate OS process, run as its own container in the docker-compose topology
(`docker/docker-compose.yml`). Nothing under `app/` may import from here, and
nothing here may import from `app/`: the two processes talk over HTTP only
(`app/execution/backend.py` is the trusted-zone client of the one endpoint
this service exposes).

Run it directly:

    .venv/Scripts/python -m execution_service

This is the only place in the entire system that imports the Docker SDK
(`docker` package) or touches a Docker socket. See `sandbox.py`.
"""

from __future__ import annotations
