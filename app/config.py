"""Central configuration for the Citadel trusted workflow zone.

Everything environment-driven lives here so that swapping an implementation
(SQLite -> Postgres, one hash strategy -> another) is a deployment change,
not a code change. Nothing in this module imports from the rest of `app`,
so it can be read by any component without creating a cycle.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# Repository root (this file is at <root>/app/config.py)
ROOT_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT_DIR / "var"


def _default_database_url() -> str:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(VAR_DIR / 'citadel.db').as_posix()}"


# --- Data Plane / persistence -------------------------------------------------
# design doc §8: "Postgres (or SQLite if faster to stand up)".
# SQLAlchemy is the seam: point this at postgresql+psycopg://... and nothing
# else in the codebase changes.
DATABASE_URL: str = os.environ.get("CITADEL_DATABASE_URL") or _default_database_url()

SQL_ECHO: bool = os.environ.get("CITADEL_SQL_ECHO", "").lower() in {"1", "true", "yes"}

# --- Observability ------------------------------------------------------------
# Which hash-chain strategy the single event writer uses. See
# app/observability/hashing.py for the registry of available strategies.
EVENT_HASH_STRATEGY: str = os.environ.get("CITADEL_EVENT_HASH_STRATEGY", "canonical_record_v1")

# previous_hash of the very first event in the chain. The design doc does not
# name a genesis value; 64 zeros is the conventional choice and is recorded
# here rather than buried in the writer.
GENESIS_HASH: str = "0" * 64


# --- Control Plane: signing keys (§6.4 Identity, §6.5 Capability) -------------
# Two *separate* secrets, not one. §2 puts Identity and Capability in the same
# process, so a shared key would work -- but a leaked capability signing key
# must not also be able to mint 8-hour session tokens, and separating them
# costs one environment variable.
#
# No development default is committed. If the variable is unset we generate a
# random per-process key: a restart then invalidates every outstanding token,
# which is the fail-closed failure mode. A checked-in "dev secret" is the
# other failure mode and it is the one that reaches production by accident.
# Any deployment where the process may restart (or where a second process must
# verify these tokens) MUST set both variables explicitly.
SESSION_SECRET: str = os.environ.get("CITADEL_SESSION_SECRET") or secrets.token_urlsafe(48)
CAPABILITY_SECRET: str = (
    os.environ.get("CITADEL_CAPABILITY_SECRET") or secrets.token_urlsafe(48)
)

#: HMAC-SHA256 -- §6.5 names the algorithm for capabilities; §6.4 says only
#: "signed", and using one algorithm for both keeps a single verification path.
JWT_ALGORITHM: str = "HS256"

#: §6.4: "issues a signed session JWT (8h expiry)".
SESSION_TTL_HOURS: int = int(os.environ.get("CITADEL_SESSION_TTL_HOURS", "8"))

#: §6.5: "short TTL (5 minutes)". This is the ONLY expiry mechanism for a
#: capability -- there is no revocation list in this slice (BB-020).
CAPABILITY_TTL_SECONDS: int = int(os.environ.get("CITADEL_CAPABILITY_TTL_SECONDS", "300"))


# --- Server entrypoint (§9, step 9's own runnable process) ---------------------
# `python -m app.main` binds a real uvicorn server here so the CLI (`cli/`) has
# something to talk to over HTTP -- the trusted zone's one process (§2), run
# for real rather than through `TestClient`. Not used by the test suite, which
# talks to `create_app()` in-process.
SERVER_HOST: str = os.environ.get("CITADEL_SERVER_HOST", "127.0.0.1")
SERVER_PORT: int = int(os.environ.get("CITADEL_SERVER_PORT", "8420"))
