"""The one uniform tool-result envelope.

Ported from the prototype's `contracts/envelopes.py`, with one change on the
way in (see below).

    // success
    {"success": true, "tool": "rag.search", "result": {"...": "..."}, "error": null,
     "metadata": {"execution_id": "..."}}

    // failure (capability OR policy OR receipt OR execution failure -- same
    // shape, different `error.code`)
    {"success": false, "tool": "rag.search", "result": null,
     "error": {"code": "POLICY_DENIED", "message": "..."}}

Both builders below emit the *same five keys*, always.

## One change on the way in: two new error codes

The prototype closed `ErrorCode` at five values and said so deliberately --
its own docstring records that a receipt-specific code was left to a later
step, because that codebase built `contracts/` before anything in it
verified a receipt on a real call path. This repo does not have that
luxury: `packages/runtime/AGENTS.md` and `packages/tools/AGENTS.md` wire
`citadel_contracts.receipts.verify_receipt` into the tool-execution
chokepoint from M0, so a caller presenting a bad, stale, or replayed
receipt needs a real error code the first time that path runs.

`receipts.py` keeps `ReceiptInvalid` and `ReceiptExpired` as two distinct
exceptions, deliberately, for the same reason `CapabilityInvalid` is kept
distinct from `CapabilityExpired` here: "your token is garbage" and "your
token was fine but timed out" call for different client behaviour (do not
blindly retry, versus refresh and retry). Collapsing both into one
`RECEIPT_INVALID` code at the envelope boundary would throw that
distinction away one layer up from where it is made. So `ErrorCode` gains
**two** values, not one: `RECEIPT_INVALID` and `RECEIPT_EXPIRED`, mirroring
the `CAPABILITY_*` pair exactly. The set is seven, not five, for this repo.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class ErrorCode:
    """The five prototype values, plus `RECEIPT_INVALID` / `RECEIPT_EXPIRED`
    (see module docstring)."""

    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_DISABLED = "TOOL_DISABLED"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    RECEIPT_INVALID = "RECEIPT_INVALID"
    RECEIPT_EXPIRED = "RECEIPT_EXPIRED"


ALL_ERROR_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.CAPABILITY_INVALID,
        ErrorCode.CAPABILITY_EXPIRED,
        ErrorCode.POLICY_DENIED,
        ErrorCode.TOOL_DISABLED,
        ErrorCode.EXECUTION_ERROR,
        ErrorCode.RECEIPT_INVALID,
        ErrorCode.RECEIPT_EXPIRED,
    }
)

#: The keys every envelope carries, success or failure.
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"success", "tool", "result", "error", "metadata"}
)


class UnknownErrorCode(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(
            f"{code!r} is not one of this repo's error codes; the set is "
            f"closed for this slice"
        )
        self.code = code


def success(
    tool: str,
    result: Mapping[str, Any],
    *,
    execution_id: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The uniform success envelope."""
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
    """The uniform failure envelope -- identical shape, whatever failed."""
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


__all__ = [
    "ErrorCode",
    "ALL_ERROR_CODES",
    "ENVELOPE_KEYS",
    "UnknownErrorCode",
    "success",
    "failure",
]
