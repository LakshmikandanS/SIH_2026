"""Orchestrator error codes (design doc section 6.2).

    "Error response: `{"error": {"code": "TASK_ALREADY_RUNNING" |
     "INVALID_REQUIREMENTS", "message": "..."}}`."

Exactly the two codes section 6.2 names for `/internal/orchestrate`. Nothing
else in this package raises a third code from this exception type -- a plan
generation failure or a model routing failure ends the task `FAILED`
directly (sections 5.2, 6.3) rather than surfacing as one of these two.
"""

from __future__ import annotations


class OrchestrationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


TASK_ALREADY_RUNNING = "TASK_ALREADY_RUNNING"
INVALID_REQUIREMENTS = "INVALID_REQUIREMENTS"
