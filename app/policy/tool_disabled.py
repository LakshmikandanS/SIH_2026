"""`DISABLE TOOL` -- the one emergency control (design doc §6.8).

    "Exactly one control: `DISABLE TOOL`. A single in-memory (or one-row-per-
     tool DB table) flag, `tool_disabled: dict[str, bool]`, checked first in
     the Policy Engine above. Toggled via
     `POST /admin/tools/{tool_name}/disable` (admin-role-only, per §6.4's
     identity model). `KILL TASK`, `KILL AGENT`, `DISABLE MODEL`,
     `GLOBAL NETWORK BLOCK`, `QUARANTINE ARTIFACT` are explicitly not built."

Nothing else from that family exists in this module, and nothing should be
added to it.

Why this is enough on its own to make §1.3 true: capabilities have no
revocation list (§6.5) -- the 5-minute TTL is their only expiry. `DISABLE TOOL`
does not try to reach already-issued tokens. It changes the *policy* answer,
and policy is consulted on every single call, so an outstanding, perfectly
valid capability stops having any effect the moment the flag flips.

In-memory, per §6.8's first option. The whole trusted workflow zone is one
process (§2), so one dict is genuinely global here. The Phase-2 seam is
`set_registry()`: a DB-backed or Redis-backed registry with the same three
methods needs no caller changes.
"""

from __future__ import annotations

import threading
from typing import Mapping, Protocol


class ToolDisabledProtocol(Protocol):
    def get(self, tool: str, default: bool = False) -> bool: ...

    def disable(self, tool: str) -> bool: ...

    def snapshot(self) -> Mapping[str, bool]: ...


class ToolDisabledRegistry:
    """`dict[str, bool]` with a lock, an audit-friendly snapshot, and no other
    powers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flags: dict[str, bool] = {}

    def get(self, tool: str, default: bool = False) -> bool:
        """Read the flag. Written to read exactly like §6.7's first line:
        `if tool_disabled.get(action.tool): return "DENY"`."""
        with self._lock:
            return self._flags.get(tool, default)

    def disable(self, tool: str) -> bool:
        """Set the flag. Returns whether it was *already* set, so the admin
        endpoint can record a no-op honestly rather than claiming a change."""
        with self._lock:
            previous = self._flags.get(tool, False)
            self._flags[tool] = True
            return previous

    def enable(self, tool: str) -> bool:
        """Clear the flag. Returns the previous value.

        **There is no HTTP endpoint for this**, and that is deliberate: §6.8
        names exactly one control and re-enable is not it. `DISABLE TOOL` is a
        one-way switch for the lifetime of the process. This method exists so
        the test suite and the step-4 demo can restore a clean slate; treating
        it as an operator affordance would be inventing a second emergency
        control.
        """
        with self._lock:
            previous = self._flags.get(tool, False)
            self._flags[tool] = False
            return previous

    def snapshot(self) -> Mapping[str, bool]:
        """Currently-disabled tools, for the admin view and event payloads."""
        with self._lock:
            return {tool: flag for tool, flag in self._flags.items() if flag}

    def clear(self) -> None:
        """Test-suite reset only."""
        with self._lock:
            self._flags.clear()


#: The module-level flag dict §6.8 describes. `app.policy.engine` reads this
#: one object; the admin endpoint writes it.
tool_disabled: ToolDisabledProtocol = ToolDisabledRegistry()


def set_registry(registry: ToolDisabledProtocol) -> ToolDisabledProtocol:
    """Swap the registry implementation (Phase-2 seam). Returns the previous
    one so it can be restored."""
    global tool_disabled
    previous = tool_disabled
    tool_disabled = registry
    return previous


def get_registry() -> ToolDisabledProtocol:
    return tool_disabled
