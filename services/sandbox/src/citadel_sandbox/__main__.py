"""`python -m citadel_sandbox` -- serve the sandbox on its internal port."""

from __future__ import annotations

import os

import uvicorn

from citadel_sandbox.app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("CITADEL_SANDBOX_HOST", "0.0.0.0"),
        port=int(os.environ.get("CITADEL_SANDBOX_PORT", "8090")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
