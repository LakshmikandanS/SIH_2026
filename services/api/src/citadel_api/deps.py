"""Process-lifetime state (`AppState`) and the one function a handler calls to learn
who is calling it (`require_user`).

Split out of `app.py`/`handlers.py` for the same reason `citadel_platform.audit.chain`
is split from `.postgres`: the *shape* of "what a request is allowed to trust" belongs
in one small, obviously-correct module a reviewer can read in one sitting, separate
from the many places that shape gets used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

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
from citadel_gateway import Gateway, build_provider
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.keyring import load_session_key
from citadel_platform.registry import Registry, load_registry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_sovereignty import HttpSandboxRunner, Sovereignty, install

#: ADR-0004: M0 targets demo-local only -- "one machine... running everything."
_DEFAULT_PROFILE = "demo-local"

#: The `PG*` variables the psql-based data layer needs, checked once, loudly, at
#: startup rather than surfacing as an opaque `psql: could not connect` later.
_REQUIRED_PG_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE")


def _repo_root() -> Path:
    # services/api/src/citadel_api/deps.py -- four parents up is the repo root,
    # computed from this file's location, never from the process cwd.
    return Path(__file__).resolve().parents[4]


class ConfigurationError(RuntimeError):
    """Something `load_app_state` needs is missing or unusable at startup. Raised, not
    guessed at: a box that refuses to start with a message naming exactly what to fix
    is better than one that starts half-configured and fails on the first request."""


@dataclass
class AppState:
    """Everything a handler needs that is not per-request, built once at startup."""

    registry: Registry
    registry_dir: Path
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    pg_env: Mapping[str, str]
    db: Database
    data_dir: DataDir
    audit: AuditLog
    tracer: Tracer
    gateway: Gateway
    sovereignty: Optional[Sovereignty]
    sandbox: Optional[HttpSandboxRunner]
    web_dir: Path


def load_app_state(env: Optional[Mapping[str, str]] = None, *, install_sovereignty: bool = True) -> AppState:
    """Build the one `AppState` this process holds for its whole lifetime. `env`
    defaults to the real environment; a test injects a plain dict."""
    environ = env if env is not None else os.environ

    profile_name = environ.get("CITADEL_PROFILE") or _DEFAULT_PROFILE
    registry_dir = Path(environ.get("CITADEL_REGISTRY_DIR") or _repo_root() / "registry")
    registry = load_registry(profile_name, registry_dir)

    missing = [key for key in _REQUIRED_PG_VARS if not environ.get(key)]
    if missing:
        raise ConfigurationError(
            f"missing {', '.join(missing)} in the environment -- start Citadel with citadel.cmd (Windows) or "
            f"scripts/run-api.sh, which set them, or export them yourself"
        )

    private_key = load_session_key(environ)
    db = Database(env=dict(environ))
    audit = AuditLog(dict(environ), registry.event_registry())
    tracer = Tracer(db)
    provider, notes = build_provider(registry, environ)
    sandbox_url = environ.get("CITADEL_SANDBOX_URL") or ""
    sovereignty = None
    if install_sovereignty:
        sovereignty = install(
            "api", db, audit=audit, env=environ,
            extra_urls=(getattr(provider, "endpoint", "") or "", sandbox_url),
        )
    return AppState(
        registry=registry,
        registry_dir=registry_dir,
        private_key=private_key,
        public_key=private_key.public_key(),
        pg_env=environ,
        db=db,
        data_dir=DataDir.from_env(environ, default=_repo_root() / ".citadel-data"),
        audit=audit,
        tracer=tracer,
        gateway=Gateway(registry, provider, audit=audit, tracer=tracer, notes=notes),
        sovereignty=sovereignty,
        sandbox=HttpSandboxRunner(sandbox_url) if sandbox_url else None,
        web_dir=Path(environ.get("CITADEL_WEB_DIR") or _repo_root() / "web" / "src"),
    )


class AuthError(Exception):
    """The bearer session token is missing, malformed, unverifiable, or expired -- one
    exception for all four; telling a caller which check failed serves no one."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Forbidden(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def require_user(request: Request, state: AppState) -> User:
    """The one function in this service a handler may use to learn who is calling it --
    root AGENTS.md invariant 3: identity comes from a verified session token, never a
    request body. Called explicitly at the top of every protected handler."""
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


def require_role(user: User, *roles: str) -> None:
    if not set(user.roles) & set(roles):
        raise Forbidden(f"this needs the {' or '.join(roles)} role; you are {', '.join(user.roles) or 'no role'}")


def user_dict(user: User) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "roles": list(user.roles),
        "clearance": user.clearance,
        "department": user.department,
    }


__all__ = [
    "AppState",
    "AuthError",
    "Forbidden",
    "ConfigurationError",
    "load_app_state",
    "require_user",
    "require_role",
    "user_dict",
]
