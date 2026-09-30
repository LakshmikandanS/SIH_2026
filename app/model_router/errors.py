"""Model Router failure modes (design doc section 6.3, C-003, BB-007, BB-008).

    "If the single local model call fails outright, the task transitions to
     FAILED. [...] A task whose classification exceeds a model's
     `max_classification` fails routing outright (`FAILED`, reason
     `MODEL_CLASSIFICATION_INCOMPATIBLE`)."

`ModelRoutingError` covers both: the *selection* failing before any model is
ever called (`MODEL_CLASSIFICATION_INCOMPATIBLE`, `NO_MODEL_FOR_CAPABILITY`)
and the actual reasoning-model call failing outright once a model has been
selected (`REASONING_CALL_FAILED`). Both map to the same outcome at the
Orchestrator: the task ends `FAILED`. Distinct `.code` values are kept so the
failure reason is legible in `/trace` rather than a single opaque string.
"""

from __future__ import annotations


class ModelRoutingError(Exception):
    """Routing (section 6.3) could not select or use a model for this task."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: design doc's own reason string, verbatim.
MODEL_CLASSIFICATION_INCOMPATIBLE = "MODEL_CLASSIFICATION_INCOMPATIBLE"

#: Defensive-only in this slice: the manifest always has exactly the two
#: capabilities the fixed scenario needs (reasoning, embedding), so this
#: never fires against the real manifest -- it exists so a caller that
#: injects a manifest missing a capability still fails closed rather than
#: raising a bare KeyError.
NO_MODEL_FOR_CAPABILITY = "NO_MODEL_FOR_CAPABILITY"

#: BB-008: no health-check, no fallback chain -- a failed local model call is
#: a failed task, reported through this code rather than a raw exception
#: escaping `app.model_router` into the Orchestrator.
REASONING_CALL_FAILED = "REASONING_CALL_FAILED"
