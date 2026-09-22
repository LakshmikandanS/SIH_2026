"""`python -m citadel_api` -- start the API (and, mounted on it, the web UI)
with uvicorn.

A two-line delegation to `main()`, on purpose: root AGENTS.md's "Current
state" records that no package in this repo had a working `python -m
citadel_platform.<pkg>` until a bug found exactly that gap, because nothing
had ever smoke-tested the `-m` invocation itself. This package gets that
entry point from its first commit instead of rediscovering the same bug.
"""

from __future__ import annotations

import os

import uvicorn

from citadel_api.app import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("CITADEL_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CITADEL_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
