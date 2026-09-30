"""Identifier generation.

The design doc (§3) shows identifiers as a component prefix plus a short
suffix: U123, T123, A123, CAP123, E001, ART123, APR123, EVT0001. This module
is the single place that shape is produced, so a later change (e.g. to ULIDs
for distributed issuance) touches one file.

Event ids are deliberately NOT generated here: they are strictly sequential
and are assigned by the Observability writer under its lock, because the
event chain's order is part of its meaning.
"""

from __future__ import annotations

import uuid

USER = "U"
TASK = "T"
AGENT = "A"
CAPABILITY = "CAP"
EVIDENCE = "E"
ARTIFACT = "ART"
APPROVAL = "APR"
PLAN = "P"

#: Not a §3 domain object: §6.6's success envelope carries
#: `metadata.execution_id` for one Tool Gateway invocation, and that id needs
#: the same shape as everything else so a trace reads uniformly.
EXECUTION = "EXEC"

EVENT_PREFIX = "EVT"
EVENT_PAD = 4  # EVT0001; ordering uses Event.seq, so >9999 events stay correct


def new_id(prefix: str) -> str:
    """Return a collision-free identifier with the given component prefix."""
    return f"{prefix}{uuid.uuid4().hex[:6].upper()}"


def format_event_id(seq: int) -> str:
    """Render an event sequence number in the doc's EVT0001 form."""
    return f"{EVENT_PREFIX}{seq:0{EVENT_PAD}d}"
