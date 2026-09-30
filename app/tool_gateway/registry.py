"""Step C of §6.6: "route to the right backend by `tool` name".

    "Agent -> Tool Gateway
       [...]
       Step C: route to the right backend by `tool` name (Data Plane, or
               Execution Service), and return one uniform envelope regardless
               of which backend answered"

A name -> callable table, and nothing else. It holds no authorization logic:
by the time `get_backend` is reached, Step A has verified a capability and
Step B has returned ALLOW. Putting a second, per-tool authorization check here
is explicitly out of scope ("no per-tool custom authorization logic outside
`decide()`").

**This is the seam steps 5-8 attach to.** A later step registers its backend
under the canonical name from `app.policy.tools.Tool` -- it does not add a
branch to the gateway, and it does not expose a second way to call the tool:

    from app.policy.tools import Tool
    from app.tool_gateway import register_backend
    register_backend(Tool.PYTHON_EXECUTE, run_in_sandbox)

Nothing is pre-registered. An unregistered tool is an `EXECUTION_ERROR`, not a
silent success: if a real backend fails to load, no stand-in answers in its
place.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from app.capability.tokens import Capability
from app.policy.context import PolicyAgent, PolicyResource, PolicyTask, PolicyUser


@dataclass(frozen=True)
class ToolRequest:
    """Everything a backend is allowed to know about one authorized call.

    It carries the *verified* capability, so a backend that needs §6.9's
    `requester` block (`classification_max`, `department`) reads it from a
    signed token rather than from the agent's arguments.
    """

    tool: str
    arguments: Mapping[str, Any]
    capability: Capability
    user: PolicyUser
    agent: PolicyAgent
    task: PolicyTask
    resource: PolicyResource
    execution_id: str

    def requester(self) -> dict[str, Any]:
        """§6.9's Data Plane `requester` block, built from the capability."""
        return {
            "task_id": self.capability.task_id,
            "agent_id": self.capability.agent_id,
            "classification_max": self.capability.scope.classification_max,
            "department": self.capability.scope.department,
        }


class ToolBackend(Protocol):
    """A backend returns the `result` body of §6.6's success envelope.

    It never builds an envelope itself -- the gateway does that, which is what
    makes the envelope uniform "regardless of which backend answered". A
    backend signals failure by raising; the gateway converts that to
    `EXECUTION_ERROR`.
    """

    def __call__(self, request: ToolRequest) -> Mapping[str, Any]: ...


class ToolNotRegistered(LookupError):
    def __init__(self, tool: str) -> None:
        super().__init__(
            f"no backend is registered for tool {tool!r}; register one with "
            f"app.tool_gateway.register_backend()"
        )
        self.tool = tool


_lock = threading.Lock()
_backends: dict[str, Callable[[ToolRequest], Mapping[str, Any]]] = {}


def register_backend(
    tool: str, backend: Callable[[ToolRequest], Mapping[str, Any]]
) -> Optional[Callable[[ToolRequest], Mapping[str, Any]]]:
    """Attach a backend to a tool name. Returns the one it replaced, if any."""
    with _lock:
        previous = _backends.get(tool)
        _backends[tool] = backend
        return previous


def unregister_backend(tool: str) -> Optional[Callable[[ToolRequest], Mapping[str, Any]]]:
    with _lock:
        return _backends.pop(tool, None)


def get_backend(tool: str) -> Callable[[ToolRequest], Mapping[str, Any]]:
    """Look up a backend. Called by `app.tool_gateway.gateway` and by nothing
    else -- that is what makes the gateway the only route to a tool."""
    with _lock:
        backend = _backends.get(tool)
    if backend is None:
        raise ToolNotRegistered(tool)
    return backend


def registered_tools() -> list[str]:
    with _lock:
        return sorted(_backends)


def clear_backends() -> None:
    """Test-suite reset only."""
    with _lock:
        _backends.clear()
