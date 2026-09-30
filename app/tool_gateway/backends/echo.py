"""The fake tool of §8's step-4 row.

    | 4 | Security path: Identity -> Policy -> Capability -> Tool Gateway,
    |     tested with a fake tool that just echoes
    |   | ALLOW and DENY both work *before* any real tool exists -- resolves C-004

That is the entire justification for this module: C-004's contradiction is
resolved *procedurally*, by proving the authorization spine against a backend
with no behaviour of its own, before the Orchestrator is ever wired to a real
tool in step 7. If the echo path is authorized correctly, the pipeline is
correct; if it is not, no real backend would have made it so.

`echo` has no side effects, no I/O, no state, and no authorization logic. It
returns its input. Anything it gets wrong is the gateway's fault, which is the
property the step-4 tests need.

**Not registered on import, anywhere.** Tests and the step-4 demo bind it
explicitly -- usually to `Tool.RAG_SEARCH`, since §6.7's rule chain only ALLOWs
the three real tool names and the point is to exercise the ALLOW path. Steps
5, 6 and 8 replace those bindings with real backends by calling
`register_backend` again. Auto-registering a fake would mean a real backend
that failed to load got silently answered by an echo.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.tool_gateway.registry import ToolRequest


class EchoFailure(Exception):
    """Raised on request, so the `EXECUTION_ERROR` branch of §6.6 can be
    exercised without a real backend that can fail."""


def echo(request: ToolRequest) -> Mapping[str, Any]:
    """Return the call back to the caller.

    Pass `arguments={"fail": true}` to make it raise instead, which is how the
    suite proves an execution failure comes back in the same envelope shape as
    a policy denial.
    """
    if request.arguments.get("fail"):
        raise EchoFailure(str(request.arguments.get("fail_message") or "echo asked to fail"))

    return {
        "echo": dict(request.arguments),
        "tool": request.tool,
        "capability_id": request.capability.capability_id,
        "requester": request.requester(),
        "resource_id": request.resource.resource_id,
    }
