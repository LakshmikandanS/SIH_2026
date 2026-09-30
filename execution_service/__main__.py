"""Entry point for running the Execution Service as its own OS process.

    .venv/Scripts/python -m execution_service

In `docker/docker-compose.yml` this is the container command for the
`execution-service` service -- the one image in the whole system that mounts
`/var/run/docker.sock`.
"""

from __future__ import annotations

import uvicorn

from execution_service import settings


def main() -> None:
    uvicorn.run(
        "execution_service.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
