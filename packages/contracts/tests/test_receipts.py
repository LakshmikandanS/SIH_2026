"""citadel_contracts/receipts.py -- the digest is canonical, a receipt is
only ever usable once, for the resource and operation it names, before it
expires, and each adversarial check below refuses for the *specific* reason
required (a wrong-resource replay refuses on the digest, not the ACL; a
replay refuses on the nonce; a stale receipt refuses on expiry; a forged
signature refuses outright).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citadel_contracts.domain import Resource
from citadel_contracts.receipts import (
    DecisionReceipt,
    InMemoryNonceStore,
    ReceiptExpired,
    ReceiptInvalid,
    new_decision_id,
    new_nonce,
    resource_digest,
    sign_receipt,
    verify_receipt,
)

MAINTENANCE_DOC = Resource.build(
    "DOC-P101-HIST", "evidence", "CONFIDENTIAL", ["maintenance"]
)
FINANCE_DOC = Resource.build(
    "DOC-FINANCE-Q3", "evidence", "CONFIDENTIAL", ["finance"]
)


# --------------------------------------------------------------------------
# resource_digest -- canonical, deterministic, order-independent on ACL
# --------------------------------------------------------------------------


def test_digest_is_deterministic_for_the_same_resource():
    assert resource_digest(MAINTENANCE_DOC) == resource_digest(MAINTENANCE_DOC)


def test_digest_differs_for_different_resources():
    assert resource_digest(MAINTENANCE_DOC) != resource_digest(FINANCE_DOC)


def test_digest_is_sha256_prefixed():
    digest = resource_digest(MAINTENANCE_DOC)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_digest_ignores_acl_order_but_not_acl_membership():
    a = Resource.build("R1", "evidence", "CONFIDENTIAL", ["maintenance", "engineering"])
    b = Resource.build("R1", "evidence", "CONFIDENTIAL", ["engineering", "maintenance"])
    c = Resource.build("R1", "evidence", "CONFIDENTIAL", ["maintenance"])
    assert resource_digest(a) == resource_digest(b)
    assert resource_digest(a) != resource_digest(c)


def test_digest_changes_if_classification_changes_and_nothing_else_does():
    higher = Resource.build("R1", "evidence", "CONFIDENTIAL", ["maintenance"])
    lower = Resource.build("R1", "evidence", "INTERNAL", ["maintenance"])
    assert resource_digest(higher) != resource_digest(lower)


def test_digest_matches_the_canonical_algorithm_by_hand():
    """Recompute the algorithm independently (sorted-key JSON, no
    whitespace, sorted ACL, UTF-8, sha256) and assert this module's output
    matches byte for byte -- not just "differs when input differs"."""
    import hashlib

    resource = Resource.build("R1", "evidence", "CONFIDENTIAL", ["b", "a"])
    payload = {
        "resource_id": "R1",
        "type": "evidence",
        "classification": "CONFIDENTIAL",
        "acl": ["a", "b"],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert resource_digest(resource) == expected


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def _make_receipt(resource, *, operation="rag.search", ttl_seconds=30, issued_at=None):
    issued = issued_at or datetime.now(timezone.utc)
    return DecisionReceipt(
        decision_id=new_decision_id(),
        task_id="T123",
        agent_id="A123",
        operation=operation,
        resource_digest=resource_digest(resource),
        scope={"classification_max": "CONFIDENTIAL", "department": "maintenance"},
        rule="tool_in_allow_list",
        issued_at=issued,
        expires_at=issued + timedelta(seconds=ttl_seconds),
        nonce=new_nonce(),
    )


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_a_freshly_issued_receipt_verifies(keypair):
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC)
    token = sign_receipt(receipt, private_key)

    verified = verify_receipt(
        token,
        public_key=public_key,
        operation="rag.search",
        resource=MAINTENANCE_DOC,
        seen_nonces=InMemoryNonceStore(),
    )
    assert verified.task_id == "T123"
    assert verified.decision_id == receipt.decision_id
    assert verified.resource_digest == resource_digest(MAINTENANCE_DOC)


def test_round_trip_preserves_every_field(keypair):
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC)
    token = sign_receipt(receipt, private_key)
    verified = verify_receipt(
        token,
        public_key=public_key,
        operation="rag.search",
        resource=MAINTENANCE_DOC,
        seen_nonces=InMemoryNonceStore(),
    )
    assert verified == receipt


# --------------------------------------------------------------------------
# the four adversarial checks -- the definition of done for this module
# --------------------------------------------------------------------------


def test_adversarial_1_replay_against_a_different_resource_refuses_on_digest_not_acl(keypair):
    """Take a valid receipt for the maintenance document and replay it
    against the finance document. It must refuse on the digest, not on the
    ACL -- proving the binding works and not merely that some filter still
    runs. There is no ACL check in this module at all (that is the
    knowledge package's job); the assertion here is that a *different
    exception reason* -- the digest -- is what verify_receipt itself
    reports."""
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC)
    token = sign_receipt(receipt, private_key)

    with pytest.raises(ReceiptInvalid, match="resource_digest"):
        verify_receipt(
            token,
            public_key=public_key,
            operation="rag.search",
            resource=FINANCE_DOC,
            seen_nonces=InMemoryNonceStore(),
        )


def test_adversarial_2_replaying_the_same_receipt_twice_refuses_on_nonce(keypair):
    """Replay a receipt twice. The second must refuse on the nonce."""
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC)
    token = sign_receipt(receipt, private_key)
    store = InMemoryNonceStore()

    verify_receipt(
        token, public_key=public_key, operation="rag.search",
        resource=MAINTENANCE_DOC, seen_nonces=store,
    )
    with pytest.raises(ReceiptInvalid, match="nonce"):
        verify_receipt(
            token, public_key=public_key, operation="rag.search",
            resource=MAINTENANCE_DOC, seen_nonces=store,
        )


def test_adversarial_3_a_receipt_used_after_it_expires_refuses_on_expiry(keypair):
    """"Hold a receipt for forty seconds, then use it. It must refuse on
    expiry." Simulated by issuing a receipt whose TTL has already elapsed,
    rather than actually sleeping forty seconds."""
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC, ttl_seconds=30)
    token = sign_receipt(receipt, private_key)

    verify_time = receipt.issued_at + timedelta(seconds=40)
    with pytest.raises(ReceiptExpired) as excinfo:
        verify_receipt(
            token, public_key=public_key, operation="rag.search",
            resource=MAINTENANCE_DOC, seen_nonces=InMemoryNonceStore(),
            now=verify_time,
        )
    # Attributable: the signature verified, so the task/agent the denial
    # belongs to is still known even though the token itself is refused.
    assert excinfo.value.receipt is not None
    assert excinfo.value.receipt.task_id == "T123"


def test_adversarial_4_a_receipt_signed_by_the_wrong_key_is_rejected_outright(keypair):
    """A backend that *skips* verification is a structural-test concern;
    this module's own equivalent -- what "not verifiable" means at the
    cryptographic level -- is a receipt signed by anyone other than the
    policy chokepoint. Ed25519 is exactly the primitive that makes this
    fail."""
    _, public_key = keypair
    attacker_key = Ed25519PrivateKey.generate()
    receipt = _make_receipt(MAINTENANCE_DOC)
    forged_token = sign_receipt(receipt, attacker_key)

    with pytest.raises(ReceiptInvalid):
        verify_receipt(
            forged_token, public_key=public_key, operation="rag.search",
            resource=MAINTENANCE_DOC, seen_nonces=InMemoryNonceStore(),
        )


# --------------------------------------------------------------------------
# operation binding
# --------------------------------------------------------------------------


def test_a_receipt_for_one_operation_does_not_authorize_another(keypair):
    private_key, public_key = keypair
    receipt = _make_receipt(MAINTENANCE_DOC, operation="rag.search")
    token = sign_receipt(receipt, private_key)

    with pytest.raises(ReceiptInvalid, match="rag.search"):
        verify_receipt(
            token, public_key=public_key, operation="python.execute",
            resource=MAINTENANCE_DOC, seen_nonces=InMemoryNonceStore(),
        )


# --------------------------------------------------------------------------
# a DENY produces no receipt -- there is nothing here to sign
# --------------------------------------------------------------------------


def test_there_is_no_such_thing_as_issuing_a_receipt_for_a_denial():
    """This module has no `deny_receipt` / `DeniedReceipt` and no code path
    that produces a signed token without a caller first constructing an
    ALLOW-shaped `DecisionReceipt`. A DENY produces no receipt at all --
    there is nothing to replay and nothing to steal. Asserted here as the
    absence of the symbol, so it cannot be reintroduced by accident."""
    import citadel_contracts.receipts as receipts_module

    assert not hasattr(receipts_module, "DeniedReceipt")
    assert not hasattr(receipts_module, "deny_receipt")
    assert not hasattr(receipts_module, "sign_denial")


# --------------------------------------------------------------------------
# structural validation of the claim set (from_claims)
# --------------------------------------------------------------------------


def test_from_claims_rejects_a_token_of_the_wrong_type():
    claims = _make_receipt(MAINTENANCE_DOC).to_claims()
    claims["typ"] = "capability"
    with pytest.raises(ReceiptInvalid, match="not a receipt token"):
        DecisionReceipt.from_claims(claims)


@pytest.mark.parametrize(
    "missing_field",
    ["decision_id", "task_id", "agent_id", "operation", "resource_digest", "rule", "nonce"],
)
def test_from_claims_rejects_a_missing_required_field(missing_field):
    claims = _make_receipt(MAINTENANCE_DOC).to_claims()
    del claims[missing_field]
    with pytest.raises(ReceiptInvalid, match=missing_field):
        DecisionReceipt.from_claims(claims)


def test_from_claims_rejects_a_non_string_scope_value():
    claims = _make_receipt(MAINTENANCE_DOC).to_claims()
    claims["scope"] = {"classification_max": 5}
    with pytest.raises(ReceiptInvalid, match="scope"):
        DecisionReceipt.from_claims(claims)


def test_from_claims_rejects_issued_at_disagreeing_with_iat():
    claims = _make_receipt(MAINTENANCE_DOC).to_claims()
    claims["iat"] = claims["iat"] + 1000
    with pytest.raises(ReceiptInvalid, match="issued_at"):
        DecisionReceipt.from_claims(claims)


def test_from_claims_rejects_expires_at_disagreeing_with_exp():
    claims = _make_receipt(MAINTENANCE_DOC).to_claims()
    claims["exp"] = claims["exp"] + 1000
    with pytest.raises(ReceiptInvalid, match="expires_at"):
        DecisionReceipt.from_claims(claims)


def test_from_claims_round_trips_a_well_formed_claim_set():
    receipt = _make_receipt(MAINTENANCE_DOC)
    assert DecisionReceipt.from_claims(receipt.to_claims()) == receipt


# --------------------------------------------------------------------------
# InMemoryNonceStore
# --------------------------------------------------------------------------


def test_nonce_store_reports_unseen_then_seen_after_record():
    store = InMemoryNonceStore()
    nonce = new_nonce()
    assert store.seen(nonce) is False
    store.record(nonce, ttl_seconds=30)
    assert store.seen(nonce) is True


def test_nonce_store_forgets_after_ttl_elapses():
    store = InMemoryNonceStore()
    nonce = new_nonce()
    store.record(nonce, ttl_seconds=0.05)
    assert store.seen(nonce) is True
    time.sleep(0.1)
    assert store.seen(nonce) is False


def test_nonce_stores_are_independent_per_instance():
    """Each verifier keeps its own nonce set. A nonce recorded in one store
    must not be visible in another."""
    store_a = InMemoryNonceStore()
    store_b = InMemoryNonceStore()
    nonce = new_nonce()
    store_a.record(nonce, ttl_seconds=30)
    assert store_a.seen(nonce) is True
    assert store_b.seen(nonce) is False
