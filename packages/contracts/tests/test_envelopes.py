"""citadel_contracts/envelopes.py -- one shape, five keys, whatever happened."""

from __future__ import annotations

import pytest

from citadel_contracts.envelopes import ENVELOPE_KEYS, ErrorCode, UnknownErrorCode, failure, success


def test_success_and_failure_have_the_same_five_keys():
    ok = success("rag.search", {"hits": []}, execution_id="EXEC1")
    bad = failure("rag.search", ErrorCode.POLICY_DENIED, "denied", execution_id="EXEC1")
    assert set(ok.keys()) == ENVELOPE_KEYS
    assert set(bad.keys()) == ENVELOPE_KEYS


def test_success_shape():
    envelope = success("rag.search", {"hits": [1, 2]}, execution_id="EXEC1")
    assert envelope["success"] is True
    assert envelope["tool"] == "rag.search"
    assert envelope["result"] == {"hits": [1, 2]}
    assert envelope["error"] is None
    assert envelope["metadata"]["execution_id"] == "EXEC1"


def test_failure_shape_carries_code_and_message_but_no_result():
    envelope = failure(
        "rag.search", ErrorCode.CAPABILITY_EXPIRED, "token expired", execution_id="EXEC2"
    )
    assert envelope["success"] is False
    assert envelope["result"] is None
    assert envelope["error"] == {"code": ErrorCode.CAPABILITY_EXPIRED, "message": "token expired"}
    assert envelope["metadata"]["execution_id"] == "EXEC2"


def test_metadata_is_present_on_failure_too():
    envelope = failure("x", ErrorCode.EXECUTION_ERROR, "boom", execution_id="EXEC3")
    assert "execution_id" in envelope["metadata"]


def test_there_are_exactly_seven_error_codes():
    """Five from the prototype, plus RECEIPT_INVALID and RECEIPT_EXPIRED --
    see the module docstring for why this repo cannot defer that pair."""
    from citadel_contracts.envelopes import ALL_ERROR_CODES

    assert len(ALL_ERROR_CODES) == 7


def test_receipt_invalid_and_receipt_expired_are_both_present_and_distinct():
    """Mirrors CAPABILITY_INVALID / CAPABILITY_EXPIRED: "your receipt is
    garbage" and "your receipt was fine but timed out" are different codes
    because they call for different client behaviour."""
    from citadel_contracts.envelopes import ALL_ERROR_CODES

    assert ErrorCode.RECEIPT_INVALID in ALL_ERROR_CODES
    assert ErrorCode.RECEIPT_EXPIRED in ALL_ERROR_CODES
    assert ErrorCode.RECEIPT_INVALID != ErrorCode.RECEIPT_EXPIRED


def test_a_receipt_invalid_failure_envelope_round_trips_like_any_other():
    envelope = failure(
        "python.execute", ErrorCode.RECEIPT_INVALID, "resource digest mismatch", execution_id="EXEC6"
    )
    assert envelope["error"]["code"] == "RECEIPT_INVALID"
    assert set(envelope.keys()) == ENVELOPE_KEYS


def test_an_unlisted_error_code_is_rejected_rather_than_passed_through():
    with pytest.raises(UnknownErrorCode):
        failure("x", "SOMETHING_MADE_UP", "boom", execution_id="EXEC4")


def test_extra_metadata_is_merged_not_replaced():
    envelope = success(
        "rag.search", {}, execution_id="EXEC5", metadata={"decision_id": "DEC123"}
    )
    assert envelope["metadata"]["execution_id"] == "EXEC5"
    assert envelope["metadata"]["decision_id"] == "DEC123"
