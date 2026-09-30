"""`POST /login` -- the one credential endpoint in the system (§6.1, §6.4).

§6.1: "`Authorization: Bearer <session JWT>`, obtained once via
`POST /login {username, password}` at CLI startup. No other credential form
exists for the MVP."
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.identity.service import AuthenticationFailed, login

router = APIRouter(tags=["identity"])


class LoginRequest(BaseModel):
    """Exactly §6.4's `{username, password}`. Note what is *not* here: there
    is no `user_id` and no `roles` a client could assert."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    """The design doc fixes the token (a signed session JWT, 8h, carrying
    `{user_id, roles}`) but not the envelope it is handed back in. This is the
    standard bearer-token shape; `user_id`/`roles`/`expires_at` are echoed so
    the CLI can show who it is logged in as without decoding the JWT itself.
    The token remains the only thing any endpoint trusts."""

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user_id: str
    roles: list[str]


@router.post("/login", response_model=LoginResponse)
def post_login(payload: LoginRequest) -> LoginResponse:
    try:
        token, expires_at, user = login(payload.username, payload.password)
    except AuthenticationFailed as exc:
        # One message for every failure mode -- no username enumeration.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "UNAUTHENTICATED", "message": str(exc)}},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    return LoginResponse(
        access_token=token,
        expires_at=expires_at,
        user_id=user.user_id,
        roles=list(user.roles),
    )
