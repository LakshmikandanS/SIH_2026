"""The CLI's session cache -- design doc §1.1 step 1's "CLI caches a
short-lived session JWT in memory for the process lifetime", pragmatically
extended to disk (docs/BUILD_LOG.md's own instruction for this step): each
shell invocation of `python -m cli ...` is a *new* process, so an
in-memory-only cache would make `citadel login` in one invocation invisible
to `citadel task` in the next -- which is not "drivable by a human" (the
mission's own bar). `citadel login` writes one small JSON file at
`~/.citadel/session.json` (or `$CITADEL_CLI_HOME`); every other command reads
it and nothing else. This is a local dev dotfile, not a credential store --
no encryption, best-effort restrictive permissions only.

This is the *only* place the CLI persists anything. No general config file
exists (non-goal); only ever this one cached token.
"""

from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass
from typing import Optional

from cli import config


class NotLoggedIn(Exception):
    """No cached session, or the cache is unreadable. `citadel login` first."""


@dataclass
class Session:
    base_url: str
    access_token: str
    token_type: str
    user_id: str
    roles: list[str]
    expires_at: str  # ISO 8601, echoed from `POST /login`'s own response


def save(session: Session) -> None:
    """Write the session cache, best-effort restricted to the owner."""
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    config.SESSION_PATH.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
    try:
        # POSIX: owner read/write only. Windows honours this only partially
        # (there is no real ACL story for a dev dotfile here) -- best-effort,
        # never fatal, per this module's own docstring.
        config.SESSION_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load() -> Session:
    """Read the cached session. Raises `NotLoggedIn` if there isn't one."""
    if not config.SESSION_PATH.exists():
        raise NotLoggedIn(f"no session cached at {config.SESSION_PATH} -- run `citadel login` first")
    try:
        raw = json.loads(config.SESSION_PATH.read_text(encoding="utf-8"))
        return Session(**raw)
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise NotLoggedIn(
            f"session cache at {config.SESSION_PATH} is unreadable ({exc}) -- run `citadel login` again"
        ) from None


def load_optional() -> Optional[Session]:
    try:
        return load()
    except NotLoggedIn:
        return None


def clear() -> None:
    config.SESSION_PATH.unlink(missing_ok=True)
