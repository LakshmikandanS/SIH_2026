"""Process-lifetime state (`AppState`) and the one function a handler calls
to learn who is calling it (`require_user`).

Split out of `app.py`/`handlers.py` for the same reason
`citadel_platform.audit.chain` is split from `.postgres`: the *shape* of
"what a request is allowed to trust" belongs in one small, obviously-correct
module a reviewer can read in one sitting, separate from the many places
that shape gets used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from starlette.requests import Request

from citadel_contracts.domain import User
from citadel_contracts.identity import (
    SessionTokenExpired,
    SessionTokenInvalid,
    verify_session_token,
)
from citadel_platform.identity.keys import load_session_signing_key
from citadel_platform.registry import Registry, load_registry

#: ADR-0004: M0 targets demo-local only -- "one machine... running
#: everything." `.env.example` names the same default. A caller that sets
#: `CITADEL_PROFILE` explicitly (e.g. to try hpc-eval's registry) is still
#: honoured; this is only what a fresh checkout gets with no setup at all.
_DEFAULT_PROFILE = "demo-local"

#: The `PG*` variables `citadel_platform._psql.run_psql_csv` needs on the
#: environment it is handed -- checked once, loudly, at startup rather than
#: surfacing as an opaque `psql: could not connect` on the first request.
_REQUIRED_PG_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE")


def _repo_root() -> Path:
    # This file lives at services/api/src/citadel_api/deps.py -- four
    # parents up is the repo root. Computed from this file's own location,
    # never from the process cwd, the same discipline scripts/lib/env.sh's
    # CITADEL_REPO_ROOT derivation uses and for the same reason: a script
    # (or, here, a server) should work no matter where it was launched from.
    return Path(__file__).resolve().parents[4]


class ConfigurationError(RuntimeError):
    """Something `load_app_state` needs is missing or unusable at startup.
    Raised, not guessed at -- a demo box that starts up in a half-configured
    state and fails on the first request it gets is worse than one that
    refuses to start at all with a message naming exactly what to fix.
    """


@dataclass(frozen=True)
class AppState:
    """Everything a handler needs that is not per-request: the loaded
    registry, the session-signing keypair, and the environment
    `citadel_platform`'s `psql`-subprocess helpers read `PG*` variables
    from. Built once by `load_app_state`, held on `app.state.citadel`
    (`app.py`), never rebuilt per request.
    """

    registry: Registry
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    pg_env: Mapping[str, str]


def load_app_state(env: Optional[Mapping[str, str]] = None) -> AppState:
    """Build the one `AppState` this process holds for its whole lifetime.

    `env` defaults to the real process environment; a caller (a test, or a
    future multi-profile launcher) can inject a plain dict instead -- the
    same `env=`/`Optional[Mapping[str, str]]` convention already used by
    `citadel_platform.migrations`, `.identity.keys` and `._psql`.
    """
    environ = env if env is not None else os.environ

    profile_name = environ.get("CITADEL_PROFILE") or _DEFAULT_PROFILE
    registry_dir_value = environ.get("CITADEL_REGISTRY_DIR")
    registry_dir = Path(registry_dir_value) if registry_dir_value else _repo_root() / "registry"
    registry = load_registry(profile_name, registry_dir)

    missing = [key for key in _REQUIRED_PG_VARS if not environ.get(key)]
    if missing:
        raise ConfigurationError(
            f"missing {', '.join(missing)} in the environment -- run "
            f"'scripts/dev-db.sh start', export the PGHOST/PGPORT/PGUSER it prints plus "
            f"PGDATABASE, then start this service again (see scripts/run-api.sh)"
        )

    private_key = load_session_signing_key(environ)
    return AppState(
        registry=registry,
        private_key=private_key,
        public_key=private_key.public_key(),
        pg_env=environ,
    )


class AuthError(Exception):
    """The bearer session token is missing, malformed, unverifiable, or
    expired. One exception for all four, mirroring
    `citadel_contracts.identity.SessionTokenInvalid`'s own reasoning:
    telling a caller which specific check failed is not a service to
    anyone. `app.py` maps every instance of this to the same 401 response.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def require_user(request: Request, state: AppState) -> User:
    """The one function in this service that a handler may use to learn who
    is calling it -- root AGENTS.md invariant 3: identity comes from a
    verified session token, never a request body.

    Called explicitly, by name, at the top of every protected handler in
    `handlers.py` -- there is deliberately no identity middleware silently
    attaching `request.state.user` behind a handler's back. `services/
    AGENTS.md`: "the discard of any caller-supplied identity field is
    visible in the code rather than implied" -- this is that same habit
    applied to the read itself: a reviewer can see, in the handler, exactly
    where the identity it acts on came from.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("missing or malformed 'Authorization: Bearer <token>' header")
    try:
        return verify_session_token(token, public_key=state.public_key)
    except SessionTokenExpired as exc:
        raise AuthError("session expired -- sign in again") from exc
    except SessionTokenInvalid as exc:
        raise AuthError(f"invalid session token: {exc}") from exc


__all__ = ["AppState", "AuthError", "ConfigurationError", "load_app_state", "require_user"]
