"""CLI-only configuration. Reads `os.environ` directly and never imports
`app.config` -- the same deliberate split `app/rag/settings.py` and
`execution_service/settings.py` already use for their own process boundaries
(docs/BUILD_LOG.md, steps 5/6): the CLI is a separate process from the
trusted-zone server it talks to, and the two must never share a settings
module just because they happen to sit in the same repo.

No general config file exists here (non-goal) -- only the base URL the CLI
points at, and where its session cache lives.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where the CLI sends every request. Must match `app.config.SERVER_HOST`/
#: `SERVER_PORT`'s default (127.0.0.1:8420) unless overridden on both sides.
DEFAULT_BASE_URL = "http://127.0.0.1:8420"
BASE_URL: str = os.environ.get("CITADEL_CLI_BASE_URL", DEFAULT_BASE_URL)

#: `/task` is synchronous and runs a whole plan -> agent loop -> tool calls to
#: completion (§6.2) -- 10-30s+ against a real model and a real container.
#: Every other call is fast; one generous timeout covers both rather than
#: special-casing `/task`.
REQUEST_TIMEOUT_SECONDS: float = float(os.environ.get("CITADEL_CLI_TIMEOUT_SECONDS", "180"))

#: The session cache directory. Overridable so tests can point it at a
#: throwaway location instead of the real developer's home directory.
HOME_DIR: Path = Path(os.environ.get("CITADEL_CLI_HOME") or (Path.home() / ".citadel"))
SESSION_PATH: Path = HOME_DIR / "session.json"
