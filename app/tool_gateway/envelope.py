"""The one uniform result envelope (design doc §6.6, closing BB-017).

    // success
    {"success": true, "tool": "rag.search", "result": {"...": "..."}, "error": null,
     "metadata": {"execution_id": "..."}}

    // failure (capability OR policy OR execution failure -- same shape,
    // different `error.code`)
    {"success": false, "tool": "rag.search", "result": null,
     "error": {"code": "POLICY_DENIED", "message": "..."}}

Both builders below emit the *same five keys*, always. §6.6's failure example
omits `metadata`, but its own sentence is "same shape ... different
`error.code`" -- so the shape is held constant and `metadata.execution_id` is
present on a failure too. That id is what ties an envelope to its
`TOOL_DENIED` / `TOOL_EXECUTED` event in `/trace`, and it is most useful
exactly when something went wrong.

`ErrorCode` is closed. §6.6 lists five codes "used in this slice" and no
component may invent a sixth: `failure()` raises on an unlisted code rather
than passing it through, the same way the event writer rejects an event type
outside §6.12's vocabulary.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class ErrorCode:
    """Exactly the five values §6.6 lists."""

    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_DISABLED = "TOOL_DISABLED"
    EXECUTION_ERROR = "EXECUTION_ERROR"


ALL_ERROR_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.CAPABILITY_INVALID,
        ErrorCode.CAPABILITY_EXPIRED,
        ErrorCode.POLICY_DENIED,
        ErrorCode.TOOL_DISABLED,
        ErrorCode.EXECUTION_ERROR,
    }
)

#: The keys every envelope carries, success or failure. Asserted by the suite
#: so "uniform" is a checked property rather than a description.
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"success", "tool", "result", "error", "metadata"}
)


class UnknownErrorCode(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(
            f"{code!r} is not one of the five error codes in design doc §6.6; "
            f"the set is closed for this slice"
        )
        self.code = code


def success(
    tool: str,
    result: Mapping[str, Any],
    *,
    execution_id: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The §6.6 success envelope."""
    meta: dict[str, Any] = {"execution_id": execution_id}
    if metadata:
        meta.update(metadata)
    return {
        "success": True,
        "tool": tool,
        "result": dict(result),
        "error": None,
        "metadata": meta,
    }


def failure(
    tool: str,
    code: str,
    message: str,
    *,
    execution_id: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The §6.6 failure envelope -- identical shape, whatever failed."""
    if code not in ALL_ERROR_CODES:
        raise UnknownErrorCode(code)
    meta: dict[str, Any] = {"execution_id": execution_id}
    if metadata:
        meta.update(metadata)
    return {
        "success": False,
        "tool": tool,
        "result": None,
        "error": {"code": code, "message": message},
        "metadata": meta,
    }
