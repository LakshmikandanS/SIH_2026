"""Observability: the single serializing event writer and its hash chain.

Import the functions from here; no component should reach past this package
into the Event table.
"""

from app.observability.event_types import ALL_EVENT_TYPES, EventType, UnknownEventType
from app.observability.writer import (
    ChainBroken,
    EventWriter,
    append_event,
    format_trace,
    get_trace,
    get_writer,
    set_writer,
    verify_chain,
)

__all__ = [
    "ALL_EVENT_TYPES",
    "ChainBroken",
    "EventType",
    "EventWriter",
    "UnknownEventType",
    "append_event",
    "format_trace",
    "get_trace",
    "get_writer",
    "set_writer",
    "verify_chain",
]
