"""Trusted-zone settings for reaching the Execution Service (design doc §2).

Matches `app/config.py`'s style -- everything environment-driven, read from
`os.environ` directly -- but is kept in its own module rather than added to
`app/config.py`, since these two settings describe an HTTP client to a
*different OS process*, not the trusted workflow zone's own configuration.

Nothing in this module imports the `docker` package, and nothing ever should:
the trusted zone reaches the isolated execution zone over HTTP only.
"""

from __future__ import annotations

import os

#: In `docker/docker-compose.yml` this resolves via the Docker network's
#: internal DNS to the `execution-service` container; for local (non-compose)
#: development it defaults to the loopback port `execution_service` binds by
#: default (see `execution_service/settings.py`).
EXECUTION_SERVICE_URL: str = os.environ.get(
    "CITADEL_EXECUTION_SERVICE_URL", "http://127.0.0.1:8901"
)

#: The HTTP client's own timeout, comfortably above the Execution Service's
#: default sandbox timeout so a slow-but-legitimate run is never cut off by
#: this layer before the sandbox's own timeout has a chance to fire.
EXECUTION_HTTP_TIMEOUT_SECONDS: float = float(
    os.environ.get("CITADEL_EXECUTION_HTTP_TIMEOUT_SECONDS", "40")
)
