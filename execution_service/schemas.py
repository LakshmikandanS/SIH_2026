"""Request/response shapes for the Execution Service's one HTTP endpoint.

The response deliberately echoes the shape of design doc §6.6's envelope
(`success` / `result` / `error` / `metadata`) even though this process has no
`tool` field to route on -- `app/execution/backend.py` is the only caller,
and giving it the same five-key shape means a failure here needs no
translation before the Tool Gateway's own envelope wraps it. This process
does not enforce identity or policy itself (§6.6 Steps A/B already ran in the
trusted zone before this HTTP call was ever made); it only executes.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    code: str = Field(..., min_length=1, description="Python source to run.")
    execution_id: Optional[str] = Field(
        default=None, description="Tool Gateway's execution id, for correlation only."
    )
    timeout_seconds: Optional[int] = Field(
        default=None, gt=0, description="Per-call override, clamped server-side."
    )


class ExecutionResultBody(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class ExecutionErrorBody(BaseModel):
    code: str
    message: str


class ExecuteResponse(BaseModel):
    success: bool
    result: Optional[ExecutionResultBody] = None
    error: Optional[ExecutionErrorBody] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
