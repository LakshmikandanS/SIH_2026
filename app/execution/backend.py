"""The python.execute Tool Gateway backend (design doc §6.6 Step C, §2).

This is the only place in `app/` that knows the Execution Service exists, and
it knows it purely as an HTTP peer -- there is no Docker SDK import here, and
there must never be one anywhere under `app/`
(`tests/test_security.py::test_execution_zone_has_no_code_path_to_the_docker_socket`
enforces that structurally).

Following `app/tool_gateway/backends/echo.py`'s contract exactly: a backend
returns the `result` body of the success envelope, or raises to signal
failure. Raising `ExecutionServiceError` here -- for an unreachable service,
a malformed response, or the sandbox itself reporting failure -- is what lets
the Tool Gateway's existing exception handling turn it into the same
`EXECUTION_ERROR` envelope every other backend failure produces. This module
builds no envelope of its own.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.execution import settings
from app.tool_gateway.registry import ToolRequest


class ExecutionServiceError(Exception):
    """The isolated execution zone could not run the call, or could not be
    reached at all. Mirrors `echo.EchoFailure`'s role for the fake tool."""


def python_execute(request: ToolRequest) -> Mapping[str, Any]:
    """Send one call's code to the Execution Service and return its result.

    `request.arguments` is agent-supplied and untrusted content by design
    (§2's whole justification for this boundary existing) -- it is forwarded
    as-is to the isolated zone and never executed here.
    """
    code = request.arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ExecutionServiceError(
            "requires a non-empty 'code' string argument"
        )

    payload: dict[str, Any] = {"execution_id": request.execution_id, "code": code}
    timeout_seconds = request.arguments.get("timeout_seconds")
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds

    try:
        response = httpx.post(
            f"{settings.EXECUTION_SERVICE_URL}/execute",
            json=payload,
            timeout=settings.EXECUTION_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ExecutionServiceError(
            f"could not reach the Execution Service at "
            f"{settings.EXECUTION_SERVICE_URL}: {exc}"
        ) from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise ExecutionServiceError(
            f"Execution Service returned a non-JSON response "
            f"(HTTP {response.status_code})"
        ) from exc

    if response.status_code != 200 or not body.get("success"):
        error = body.get("error") or {}
        message = error.get("message") or (
            f"Execution Service call failed (HTTP {response.status_code})"
        )
        raise ExecutionServiceError(message)

    result = body.get("result") or {}
    return {
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "exit_code": result.get("exit_code"),
    }


def register() -> None:
    """Attach this backend the same way every other tool does (§6.6):

        from app.tool_gateway import register_backend
        register_backend(Tool.<...>, <this module's callable>)

    Kept as a one-line helper so callers (tests, the demo, and eventually the
    Orchestrator's process startup) import a function instead of repeating
    the tool constant.
    """
    from app.policy import Tool
    from app.tool_gateway import register_backend

    register_backend(Tool.PYTHON_EXECUTE, python_execute)
