"""The local identity provider (design doc §6.4).

One table of users with bcrypt-hashed passwords, standing in for LDAP/AD. The
whole of the credential check is `authenticate()` below; a real deployment
replaces that one function with a directory bind and everything above it --
`/login`, the session JWT, every `Depends(current_user)` -- is unchanged.

No event is emitted on login: §6.12 fixes a closed 16-type vocabulary for this
slice and none of them is an authentication event. Adding one would widen a
contract this step does not own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ids
from app.db.engine import SessionLocal
from app.db.models import User
from app.identity.passwords import hash_password, verify_password
from app.identity.tokens import SessionIdentity, issue_session_token


class AuthenticationFailed(Exception):
    """Bad username, bad password, or a user with no credential set.

    Deliberately one exception with one message: telling a caller *which* of
    those it was is a username-enumeration oracle.
    """

    def __init__(self) -> None:
        super().__init__("invalid username or password")


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    roles: Sequence[str],
    clearance: str,
    department: str,
    user_id: Optional[str] = None,
) -> User:
    """Seed a user row. Does not commit -- the caller owns the transaction
    (the house rule established with `app.db.transitions`).

    This is the enrolment path for the demo; there is no self-service
    registration endpoint in this slice.
    """
    user = User(
        user_id=user_id or ids.new_id(ids.USER),
        username=username,
        roles=list(roles),
        clearance=clearance,
        department=department,
        password_hash=hash_password(password),
    )
    session.add(user)
    session.flush()
    return user


def get_user(session: Session, user_id: str) -> Optional[User]:
    return session.get(User, user_id)


def get_user_by_username(session: Session, username: str) -> Optional[User]:
    return session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()


def authenticate(session: Session, username: str, password: str) -> User:
    """THE credential check. Raises `AuthenticationFailed` on any failure.

    Replace this body with an LDAP/AD bind and the rest of the system is
    untouched -- that is the whole point of routing every caller through here.
    """
    user = get_user_by_username(session, username)
    if user is None or not verify_password(password, user.password_hash):
        raise AuthenticationFailed()
    return user


def login(username: str, password: str) -> tuple[str, datetime, User]:
    """Authenticate and mint the 8-hour session JWT of §6.4.

    Returns `(token, expires_at, user)`. Opens its own read-only session: no
    state changes, so there is nothing for a caller to enlist in.
    """
    with SessionLocal() as session:
        user = authenticate(session, username, password)
        token, expires_at = issue_session_token(user.user_id, user.roles)
        session.expunge(user)
        return token, expires_at, user


def load_identity_user(identity: SessionIdentity) -> Optional[User]:
    """Resolve the verified session identity to its User row.

    Used where a component needs `clearance`/`department` (the Policy Engine's
    inputs) rather than just `user_id`/`roles`. The lookup key comes from the
    *token*, never from a request body.
    """
    with SessionLocal() as session:
        user = session.get(User, identity.user_id)
        if user is not None:
            session.expunge(user)
        return user
