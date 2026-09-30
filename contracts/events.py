"""The event vocabulary of design doc §6.12.

Ported unchanged from `app/observability/event_types.py` (P1 §1). This is a
closed set for the slice. The single Observability writer's `append_event`
rejects anything not in it -- fail closed (AGENTS.md §9): an unrecognised
event type is a caller bug, and silently accepting it would put an
unauditable row in the chain.

This module stays a pure vocabulary -- it does not gain a `RECEIPT_*` event
type in this step. P1 §5 keeps the Tool Gateway's event emissions unchanged
(`CAPABILITY_CHECKED`, `POLICY_DECISION`, `TOOL_DENIED`, `TOOL_EXECUTED`); the
`POLICY_DECISION` payload gains a `decision_id` field, not a new event type.
"""

from __future__ import annotations


class EventType:
    TASK_CREATED = "TASK_CREATED"
    PLAN_CREATED = "PLAN_CREATED"
    AGENT_STARTED = "AGENT_STARTED"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    CAPABILITY_CHECKED = "CAPABILITY_CHECKED"
    POLICY_DECISION = "POLICY_DECISION"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_DENIED = "TOOL_DENIED"
    EVIDENCE_RETRIEVED = "EVIDENCE_RETRIEVED"
    STATE_COMMITTED = "STATE_COMMITTED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    ARTIFACT_RELEASED = "ARTIFACT_RELEASED"


#: Exactly the 16 types listed in design doc §6.12, in that order.
ALL_EVENT_TYPES: tuple[str, ...] = (
    EventType.TASK_CREATED,
    EventType.PLAN_CREATED,
    EventType.AGENT_STARTED,
    EventType.ACTION_REQUESTED,
    EventType.CAPABILITY_CHECKED,
    EventType.POLICY_DECISION,
    EventType.TOOL_EXECUTED,
    EventType.TOOL_DENIED,
    EventType.EVIDENCE_RETRIEVED,
    EventType.STATE_COMMITTED,
    EventType.ARTIFACT_CREATED,
    EventType.ARTIFACT_VERIFIED,
    EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_GRANTED,
    EventType.APPROVAL_REJECTED,
    EventType.ARTIFACT_RELEASED,
)

EVENT_TYPE_SET = frozenset(ALL_EVENT_TYPES)


class UnknownEventType(ValueError):
    def __init__(self, event_type: str) -> None:
        super().__init__(
            f"{event_type!r} is not one of the 16 event types in design doc "
            f"§6.12; the vocabulary is closed for this slice"
        )
        self.event_type = event_type


__all__ = ["EventType", "ALL_EVENT_TYPES", "EVENT_TYPE_SET", "UnknownEventType"]
