"""Control Plane -- Identity (design doc §6.4).

A single local table of bcrypt-hashed users, standing in for LDAP/AD, plus the
8-hour session JWT every other endpoint derives its acting identity from.

Import from here; nothing outside this package should touch `password_hash` or
decode a session token by hand.
"""

from app.identity.dependencies import (
    CLIENT_IDENTITY_FIELDS,
    current_identity,
    drop_client_identity,
    optional_json_body,
    require_role,
)
from app.identity.passwords import PasswordTooLong, hash_password, verify_password
from app.identity.service import (
    AuthenticationFailed,
    authenticate,
    create_user,
    get_user,
    get_user_by_username,
    load_identity_user,
    login,
)
from app.identity.tokens import (
    SessionIdentity,
    SessionInvalid,
    issue_session_token,
    verify_session_token,
)

__all__ = [
    "AuthenticationFailed",
    "CLIENT_IDENTITY_FIELDS",
    "PasswordTooLong",
    "SessionIdentity",
    "SessionInvalid",
    "authenticate",
    "create_user",
    "current_identity",
    "drop_client_identity",
    "get_user",
    "get_user_by_username",
    "hash_password",
    "issue_session_token",
    "load_identity_user",
    "login",
    "optional_json_body",
    "require_role",
    "verify_password",
    "verify_session_token",
]
