"""Trusted-zone client of the Execution Service (design doc §2, §6.6 Step C).

Everything under `app/` (including this package) runs inside the trusted
workflow zone and must never hold a Docker socket or import the `docker`
package -- that is the whole point of the process boundary the design doc
draws in §2. This package speaks HTTP only. The process that actually owns
containers lives in the separate top-level `execution_service/` package.

Attach the backend the same way every other tool does:

    from app.policy import Tool
    from app.tool_gateway import register_backend
    from app.execution import register

    register()  # register_backend(Tool.PYTHON_EXECUTE, python_execute)
"""

from __future__ import annotations

from app.execution.backend import ExecutionServiceError, python_execute, register

__all__ = ["ExecutionServiceError", "python_execute", "register"]
