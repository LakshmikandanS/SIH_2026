"""FastAPI wiring for §6.4's non-negotiable rule.

    "Every subsequent endpoint derives the acting identity from this verified
     JWT -- a client-supplied `user_id` in a request body is never trusted and
     is ignored if present. This applies with no exception to the approval
     endpoint (§6.10): `approver_id` always comes from the session, never from
     the request payload."

Two things live here:

  * `current_identity` / `require_role` -- the only sanctioned way for an
    endpoint to learn who is calling. Both return a `SessionIdentity`, which
    can only be produced by `verify_session_token`.
  * `drop_client_identity` -- the explicit discard of any identity field a
    client put in a body. Endpoints call it so the rule is *visible in the
    code*, not merely implied by the absence of a read.

Later steps (the approval endpoint, §6.10) must use these; there is no second
route to an identity in this codebase.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.identity.tokens import SessionIdentity, SessionInvalid, verify_session_token

#: Identity fields a client might try to assert in a request body. Every one
#: of them is derived from the session instead. `approver_id` is listed here
#: because §6.10 calls it out by name.
CLIENT_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"user_id", "approver_id", "actor_id", "agent_id", "roles"}
)

_bearer_scheme = HTTPBearer(auto_error=False, description="Session JWT from POST /login")


def current_identity(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> SessionIdentity:
    """The acting identity for this request, from the verified Bearer JWT.

    401 on anything wrong with the token. There is no anonymous fallback and
    no header other than `Authorization: Bearer <session JWT>` (§6.1: "No
    other credential form exists for the MVP").
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "UNAUTHENTICATED", "message": "no session token"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_session_token(credentials.credentials)
    except SessionInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "UNAUTHENTICATED", "message": str(exc)}},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def require_role(role: str) -> Callable[..., SessionIdentity]:
    """Dependency factory: 403 unless the *session's* roles contain `role`.

    Roles come from the signed token, so a caller cannot grant itself `admin`
    by putting `"roles": ["admin"]` in a body -- §6.8's admin-only
    `DISABLE TOOL` rests on exactly that.
    """

    def _dependency(
        identity: SessionIdentity = Depends(current_identity),
    ) -> SessionIdentity:
        if not identity.has_role(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "FORBIDDEN",
                        "message": f"role {role!r} is required for this operation",
                    }
                },
            )
        return identity

    return _dependency


def drop_client_identity(body: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return `body` with every client-asserted identity field removed.

    The fields are not rejected, they are *ignored* -- §6.4's wording is
    "never trusted and is ignored if present", so a request carrying one still
    succeeds, it just has no effect on who the caller is.
    """
    if not body:
        return {}
    return {k: v for k, v in body.items() if k not in CLIENT_IDENTITY_FIELDS}


async def optional_json_body(request: Request) -> dict[str, Any]:
    """Read a JSON body if one was sent, tolerating an empty or non-JSON one.

    Used by endpoints that take no input but must still demonstrably ignore
    identity fields a client attaches.
    """
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
