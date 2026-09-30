"""Orchestrator (design doc section 5, section 6.2) -- including the Query
Router's task-intake role, which is not a separate service for this MVP.

Task intake (`POST /task`) -> the canonical handoff (`POST /internal/orchestrate`,
section 6.2) -> plan generation (section 5.2) -> the agent execution loop
(section 5.1), routed through the Tool Gateway built by
`security-control-plane` -> one scoped plan-revision case (section 5.3).

The Orchestrator is the sole owner of task status from `/internal/orchestrate`
onward (section 6.2, C-005): `GET /tasks/{id}` and `GET /tasks/{id}/trace`
live only in `app.orchestrator.router`.
"""

from app.orchestrator.errors import (
    INVALID_REQUIREMENTS,
    TASK_ALREADY_RUNNING,
    OrchestrationError,
)
from app.orchestrator.plan import PlanGenerationFailed, generate_plan
from app.orchestrator.revision import RevisionNotPermitted, RevisionOutcome, revise_report
from app.orchestrator.schemas import OrchestratePayload, PlanModel, PlanStepModel, TaskCreateRequest
from app.orchestrator.service import OrchestrateResult, orchestrate

__all__ = [
    "INVALID_REQUIREMENTS",
    "TASK_ALREADY_RUNNING",
    "OrchestrateResult",
    "OrchestratePayload",
    "OrchestrationError",
    "PlanGenerationFailed",
    "PlanModel",
    "PlanStepModel",
    "RevisionNotPermitted",
    "RevisionOutcome",
    "TaskCreateRequest",
    "generate_plan",
    "orchestrate",
    "revise_report",
]
