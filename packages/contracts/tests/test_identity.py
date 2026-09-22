"""citadel_contracts/identity.py -- a session token round-trips a User exactly,
refuses when forged, refuses when expired, and refuses when it is some other
token kind wearing a valid signature (a receipt, say). Structured like
test_receipts.py's adversarial section: each refusal is checked for the
*specific* reason required, not just "raises something".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citadel_contracts.domain import User
from citadel_contracts.identity import (
    DEFAULT_TTL_SECONDS,
    TOKEN_TYPE,
    SessionTokenExpired,
    SessionTokenInvalid,
    issue_session_token,
    verify_session_token,
)

ENGINEER = User(
    user_id="U-ENG-1",
    username="r.kulkarni",
    roles=("engineer",),
    clearance="internal",
    department="process-engineering",
)


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_a_freshly_issued_session_verifies(keypair):
    private_key, public_key = keypair
    token = issue_session_token(ENGINEER, private_key=private_key)

    user = verify_session_token(token, public_key=public_key)

    assert user == ENGINEER


def test_round_trip_preserves_every_field(keypair):
    private_key, public_key = keypair
    user = User(
        user_id="U-APP-1",
        username="v.rangan",
        roles=("approver", "engineer"),
        clearance="confidential",
        department="quality-assurance",
    )

    token = issue_session_token(user, private_key=private_key)
    verified = verify_session_token(token, public_key=public_key)

    assert verified.user_id == "U-APP-1"
    assert verified.username == "v.rangan"
    assert verified.roles == ("approver", "engineer")
    assert verified.clearance == "confidential"
    assert verified.department == "quality-assurance"
    # created_at deliberately does not round-trip -- it is not part of the
    # session claims (a login timestamp, not a token-issuance timestamp; the
    # two are different moments the instant a session is refreshed).
    assert verified.created_at is None


def test_a_user_with_no_roles_still_verifies():
    """Zero roles is a valid, if useless, identity -- it simply cannot pass
    any role-gated policy rule later. That is the policy evaluator's job to
    enforce, not this module's."""
    private_key = Ed25519PrivateKey.generate()
    bare = User(user_id="U-X", username="nobody", clearance="public", department="none")

    token = issue_session_token(bare, private_key=private_key)
    user = verify_session_token(token, public_key=private_key.public_key())

    assert user.roles == ()


def test_ttl_defaults_to_an_eight_hour_workday(keypair):
    private_key, public_key = keypair
    issued_at = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    token = issue_session_token(ENGINEER, private_key=private_key, now=issued_at)

    # Still valid one second before the default TTL elapses...
    verify_session_token(
        token,
        public_key=public_key,
        now=issued_at + timedelta(seconds=DEFAULT_TTL_SECONDS - 1),
    )
    # ...expired one second after.
    with pytest.raises(SessionTokenExpired):
        verify_session_token(
            token,
            public_key=public_key,
            now=issued_at + timedelta(seconds=DEFAULT_TTL_SECONDS + 1),
        )


# ---------------------------------------------------------------------------
# adversarial
# ---------------------------------------------------------------------------


def test_adversarial_1_a_session_signed_by_the_wrong_key_is_rejected_outright(keypair):
    _, public_key = keypair
    attacker_key = Ed25519PrivateKey.generate()
    forged = issue_session_token(ENGINEER, private_key=attacker_key)

    with pytest.raises(SessionTokenInvalid):
        verify_session_token(forged, public_key=public_key)


def test_adversarial_2_an_expired_session_refuses_on_expiry_and_still_names_the_user(keypair):
    private_key, public_key = keypair
    issued_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = issue_session_token(
        ENGINEER, private_key=private_key, ttl_seconds=60, now=issued_at
    )

    with pytest.raises(SessionTokenExpired) as excinfo:
        verify_session_token(
            token, public_key=public_key, now=issued_at + timedelta(hours=1)
        )

    # The signature verified; only the clock failed. The identity is still
    # recoverable so a denial can be attributed to the right person -- same
    # reasoning as ReceiptExpired.receipt.
    assert excinfo.value.user == ENGINEER


def test_adversarial_3_a_receipt_token_is_not_a_valid_session(keypair):
    """The cross-token-kind check: something signed with the right key but
    carrying `typ: "receipt"` (or any typ other than "session") must be
    refused, not silently accepted because the signature happens to verify.
    This is what stops a receipt -- a real, validly-signed token in this
    system -- from being replayable as a session."""
    private_key, public_key = keypair
    import jwt as pyjwt

    not_a_session = pyjwt.encode(
        {"typ": "receipt", "sub": "U-ENG-1", "iat": 0, "exp": 9999999999},
        private_key,
        algorithm="EdDSA",
    )

    with pytest.raises(SessionTokenInvalid, match="not a session token"):
        verify_session_token(not_a_session, public_key=public_key)


def test_adversarial_4_a_blank_token_is_rejected():
    with pytest.raises(SessionTokenInvalid):
        verify_session_token("", public_key=Ed25519PrivateKey.generate().public_key())


def test_adversarial_5_tampering_with_the_payload_invalidates_the_signature(keypair):
    private_key, public_key = keypair
    token = issue_session_token(ENGINEER, private_key=private_key)
    header, payload, signature = token.split(".")

    tampered = f"{header}.{payload}x.{signature}"

    with pytest.raises(SessionTokenInvalid):
        verify_session_token(tampered, public_key=public_key)


# ---------------------------------------------------------------------------
# claim validation -- malformed but well-signed claims are refused, not
# accepted with a wrong-typed field silently coerced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken_claims",
    [
        {"typ": TOKEN_TYPE, "username": "x", "iat": 0, "exp": 9999999999},  # no sub
        {"typ": TOKEN_TYPE, "sub": "U1", "iat": 0, "exp": 9999999999},  # no username
        {
            "typ": TOKEN_TYPE,
            "sub": "U1",
            "username": "x",
            "roles": "engineer",  # not a list
            "iat": 0,
            "exp": 9999999999,
        },
        {
            "typ": TOKEN_TYPE,
            "sub": "U1",
            "username": "x",
            "clearance": 7,  # not a string
            "iat": 0,
            "exp": 9999999999,
        },
    ],
)
def test_malformed_but_validly_signed_claims_are_refused(keypair, broken_claims):
    private_key, public_key = keypair
    import jwt as pyjwt

    token = pyjwt.encode(broken_claims, private_key, algorithm="EdDSA")

    with pytest.raises(SessionTokenInvalid):
        verify_session_token(token, public_key=public_key)
