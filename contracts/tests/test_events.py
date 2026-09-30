"""contracts/events.py -- the vocabulary is exactly the 16 types of design
doc §6.12, and it is closed."""

from __future__ import annotations

import pytest

from contracts.events import ALL_EVENT_TYPES, EVENT_TYPE_SET, EventType, UnknownEventType


def test_there_are_exactly_sixteen_event_types():
    assert len(ALL_EVENT_TYPES) == 16
    assert len(EVENT_TYPE_SET) == 16


def test_the_set_and_the_ordered_tuple_agree():
    assert EVENT_TYPE_SET == frozenset(ALL_EVENT_TYPES)


def test_capability_checked_and_policy_decision_are_both_present():
    """These two are what P1 wires a receipt's issuance and verification
    into -- the Tool Gateway's existing emissions stay unchanged (P1 §5)."""
    assert EventType.CAPABILITY_CHECKED in EVENT_TYPE_SET
    assert EventType.POLICY_DECISION in EVENT_TYPE_SET
    assert EventType.TOOL_DENIED in EVENT_TYPE_SET
    assert EventType.TOOL_EXECUTED in EVENT_TYPE_SET


def test_the_vocabulary_has_no_receipt_specific_event_type_yet():
    """P1 §5: the POLICY_DECISION payload gains a decision_id field, not a
    new event type. If a RECEIPT_* constant appears here before a later
    phase actually needs one, that is scope creep this test should catch."""
    assert not any(name.startswith("RECEIPT_") for name in EVENT_TYPE_SET)


def test_an_unrecognised_event_type_is_rejected():
    with pytest.raises(UnknownEventType):
        raise UnknownEventType("SOMETHING_MADE_UP")
