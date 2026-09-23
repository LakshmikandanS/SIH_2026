"""The audit writer every package uses: registered event names only.

`citadel_contracts.events.EventRegistry` promises that "the audit writer's append
will still reject an unrecognised name and refuse to put an unauditable row in the
chain". This is that writer. It checks the name against the registry populated from
`registry/events.yaml` at startup, then appends through `audit.psql_client` -- so a
typo'd event name fails the operation that tried to record it, loudly, instead of
landing a row nobody's tooling knows how to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from citadel_contracts.events import EventRegistry

from citadel_platform.audit.psql_client import append_via_psql


@dataclass(frozen=True)
class AuditLog:
    env: Mapping[str, str]
    events: EventRegistry

    def record(self, event_name: str, *, actor_id: Optional[str], payload: Mapping[str, Any]) -> int:
        self.events.assert_registered(event_name)
        return append_via_psql(self.env, event_name=event_name, actor_id=actor_id, payload=dict(payload))


__all__ = ["AuditLog"]
