"""The sandbox service: the only process that executes model-authored code.

A small Starlette app in its own container on an internal-only network with no route
out. `POST /run` verifies the receipt with the receipt *public* key and this process's
own nonce store, then runs the source through `citadel_tools.sandbox` -- a fresh
directory per run, a separate process, resource limits, a timeout, and the in-process
guard as defence in depth. `POST /probe` is the deliberate egress probe run from inside
the sandbox's own network namespace. `GET /health` says what this sandbox is.
"""

from __future__ import annotations

__all__: list[str] = []
