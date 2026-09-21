"""The event vocabulary -- an open registry, not a closed enum.

The prototype's `contracts/events.py` closed this at exactly sixteen
`EventType` string constants and rejected anything else. This repo's own
root `AGENTS.md` names that pattern as a failure mode by example: "closed
vocabulary" -- a fixed `StrEnum` of event types that cannot grow without a
code change. `packages/contracts/AGENTS.md`'s porting table is explicit
about what carries over from `events.py`: "the discipline, not the file...
A registry with registration, not a closed StrEnum." This module is that
registry.

`citadel_contracts` ships **no event names of its own**. Naming the events
this deployment cares about is `registry/events.yaml`'s job -- data, not
code, exactly like `registry/policy.yaml` and `registry/profiles.yaml` --
and reading that file at startup is `citadel_platform`'s job, not this
package's: `citadel_contracts` has zero inward dependencies and does no
file I/O, ever (`tests/structural/test_contracts_is_self_contained.py`).
So the flow is: `citadel_platform` reads `registry/events.yaml` once at
startup and calls `register_many()` on the one `EventRegistry` the process
shares; only after that does anything call `assert_registered()`, and it
still fails closed on anything that startup step never saw -- the audit
writer's `append_event` will still reject an unrecognised name and refuse
to put an unauditable row in the chain, exactly as the prototype's fixed
vocabulary did, just checked against an open, deployment-controlled table
instead of a closed, release-frozen one.

Not a module-level singleton. `EventRegistry` is instantiated by whoever
needs one -- production: `citadel_platform`, once; tests: as many
independent instances as a test needs -- the same reason
`receipts.InMemoryNonceStore` is instantiated rather than reached for as a
global: shared mutable vocabulary state is exactly the kind of thing that
should not leak between test modules, or between two deployments in the
same process.
"""

from __future__ import annotations

import threading
from typing import Iterable


class UnknownEventType(ValueError):
    def __init__(self, event_type: str) -> None:
        super().__init__(
            f"{event_type!r} was never registered in this EventRegistry; "
            f"the vocabulary is open but not unchecked -- register it "
            f"(registry/events.yaml) before anything can emit it"
        )
        self.event_type = event_type


class EventRegistry:
    """An open, append-only vocabulary of event type names.

    Deliberately not a closed `StrEnum` (see module docstring). Deliberately
    also not unchecked: an event type must be registered before
    `assert_registered` will pass it, so the audit writer still fails
    closed on a caller's typo -- just against a table the deployment
    controls, not one frozen into this package at release time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._types: set[str] = set()

    def register(self, event_type: str) -> None:
        """Idempotent: registering the same name twice is not an error --
        two packages naming the same event at startup is normal, not a
        collision."""
        if not isinstance(event_type, str) or not event_type:
            raise ValueError(
                f"event type must be a non-empty string, got {event_type!r}"
            )
        with self._lock:
            self._types.add(event_type)

    def register_many(self, event_types: Iterable[str]) -> None:
        for event_type in event_types:
            self.register(event_type)

    def is_registered(self, event_type: str) -> bool:
        with self._lock:
            return event_type in self._types

    def assert_registered(self, event_type: str) -> str:
        """Fail closed: an unrecognised event type is a caller bug, and
        silently accepting it would put an unauditable row in the chain --
        the prototype's own reasoning, kept, applied to an open table
        instead of a closed one."""
        if not self.is_registered(event_type):
            raise UnknownEventType(event_type)
        return event_type

    @property
    def all_types(self) -> frozenset[str]:
        """A snapshot, not a live view -- registering more afterwards does
        not retroactively change a snapshot already taken."""
        with self._lock:
            return frozenset(self._types)

    def __len__(self) -> int:
        with self._lock:
            return len(self._types)

    def __contains__(self, event_type: object) -> bool:
        return isinstance(event_type, str) and self.is_registered(event_type)


__all__ = ["EventRegistry", "UnknownEventType"]
