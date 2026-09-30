"""Plan generation (design doc section 5.2, BB-002).

    "One structured-output call to the Model Router's reasoning model, using
     a fixed prompt template and a JSON Schema the response is validated
     against [...] If the model's output fails schema validation, one repair
     prompt is sent [...] a second failure marks the task `FAILED`. No
     planning algorithm beyond this exists for the MVP -- the plan shape for
     this scenario is effectively fixed, and the LLM call exists to prove the
     mechanism, not to demonstrate open-ended planning."

The prompt is fixed to this slice's one scenario (design doc section 0) --
not built from a caller-supplied free-text task description. section 6.2's
own canonical `/internal/orchestrate` payload does not carry one either
(only `task_id`/`user_id`/`classification`/`task_type`/`requirements`), which
is the strongest signal the design doc gives that planning in this slice is
keyed off `task_type`, not off open-ended user text. `/task`'s free-text
argument is still accepted and recorded on `TASK_CREATED` (so a human reading
`/trace` sees what was actually asked for), it just is not what drives the
structured-output call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError

from app import ids
from app.model_router import (
    ModelRoutingError,
    ReasoningModelError,
    generate_json,
    route_models,
)
from app.model_router.manifest import MODEL_MANIFEST
from app.orchestrator.schemas import PlanModel, validate_fixed_shape
from app.policy import Tool

#: design doc section 0's own scenario sentence, verbatim -- the one thing
#: this slice's plan is ever generated for.
_SCENARIO_TEXT = (
    "Using the available internal maintenance documents, identify the "
    "recent maintenance history of Pump P-101 and generate a short "
    "maintenance summary report."
)

_EXAMPLE_SHAPE = """{
  "plan_id": "P000001",
  "task_id": "<the task id given below>",
  "steps": [
    {"step_id": "S1", "agent_type": "researcher", "action": "%(rag)s",
     "arguments": {"query": "Pump P-101 maintenance history"}},
    {"step_id": "S2", "agent_type": "researcher", "action": "%(exec)s",
     "arguments": {"code": "<python source -- recomputed by the agent at runtime>"}},
    {"step_id": "S3", "agent_type": "writer", "action": "%(report)s",
     "arguments": {"template": "maintenance_summary_v1"}}
  ]
}"""


class PlanGenerationFailed(Exception):
    """Section 5.2: model call failure, or two consecutive schema-validation
    failures (the fixed prompt, then exactly one repair prompt). Either
    outcome ends the task `FAILED` -- the Orchestrator (`app.orchestrator.service`)
    is what performs that transition; this module only reports why."""


@dataclass(frozen=True)
class PlanGenerationResult:
    plan: PlanModel
    model_id: str
    routing_reason: str
    repaired: bool


def _example_shape(rag_tool: str, exec_tool: str, report_tool: str) -> str:
    return _EXAMPLE_SHAPE % {"rag": rag_tool, "exec": exec_tool, "report": report_tool}


def _build_prompt(task_id: str, rag_tool: str, exec_tool: str, report_tool: str) -> str:
    return (
        "You are Citadel's planning module. Produce a plan as a single JSON "
        "object and nothing else -- no prose, no markdown fences -- matching "
        "exactly this shape, with these three steps in this exact order:\n\n"
        f"{_example_shape(rag_tool, exec_tool, report_tool)}\n\n"
        f"task_id: {task_id}\n"
        f"Task: {_SCENARIO_TEXT}\n\n"
        "Respond with ONLY the JSON object."
    )


def _repair_prompt(
    task_id: str, previous_output: str, error: str, rag_tool: str, exec_tool: str, report_tool: str
) -> str:
    schema = json.dumps(PlanModel.model_json_schema())
    return (
        "Your last output was invalid JSON against this schema: "
        f"{schema}\n\nValidation error: {error}\n\n"
        f"Your previous output was:\n{previous_output}\n\n"
        "Reply again with ONLY a single valid JSON object matching this "
        f"exact shape, three steps, in this exact order:\n\n"
        f"{_example_shape(rag_tool, exec_tool, report_tool)}\n\n"
        f"task_id: {task_id}\n"
        f"Task: {_SCENARIO_TEXT}\n\n"
        "Respond with ONLY the JSON object."
    )


def _try_parse(raw: str) -> tuple[Optional[PlanModel], Optional[str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    try:
        plan = PlanModel.model_validate(data)
    except ValidationError as exc:
        return None, str(exc)
    try:
        validate_fixed_shape(plan)
    except ValueError as exc:
        return None, str(exc)
    return plan, None


def generate_plan(task_id: str, classification: str) -> PlanGenerationResult:
    """section 5.2, end to end: route to the reasoning model, one
    structured-output call, validate, one repair attempt on failure.

    Raises `PlanGenerationFailed` (never a bare exception from
    `app.model_router` or `pydantic`) on any terminal failure -- routing
    failure, an unreachable model, or two consecutive invalid outputs.
    """
    try:
        routing = route_models(
            task_id=task_id, required_capabilities=["reasoning"], classification=classification
        )
    except ModelRoutingError as exc:
        raise PlanGenerationFailed(f"model routing failed: {exc.code}: {exc.message}") from exc

    model_id = routing.selected_models.get("reasoning")
    if not model_id:
        raise PlanGenerationFailed(
            "model routing did not select a reasoning model for the planning call"
        )
    # The Model Router's selection is a *logical* id (section 6.3's own
    # example: "qwen3-local"); resolving it to the real Ollama model name is
    # `app.model_router`'s job, not this module's -- but the call itself
    # still has to name a model Ollama actually understands, so look the
    # manifest entry back up here rather than duplicating the mapping.
    manifest_entry = MODEL_MANIFEST.get(model_id)
    ollama_model = manifest_entry.ollama_model if manifest_entry else model_id

    prompt = _build_prompt(task_id, Tool.RAG_SEARCH, Tool.PYTHON_EXECUTE, Tool.GENERATE_REPORT)

    try:
        raw = generate_json(model=ollama_model, prompt=prompt)
    except ReasoningModelError as exc:
        raise PlanGenerationFailed(f"reasoning model call failed: {exc}") from exc

    plan, error = _try_parse(raw)
    repaired = False
    if plan is None:
        repaired = True
        repair = _repair_prompt(
            task_id, raw, error or "unknown error", Tool.RAG_SEARCH, Tool.PYTHON_EXECUTE, Tool.GENERATE_REPORT
        )
        try:
            raw2 = generate_json(model=ollama_model, prompt=repair)
        except ReasoningModelError as exc:
            raise PlanGenerationFailed(
                f"reasoning model call failed during the one repair attempt: {exc}"
            ) from exc
        plan, error2 = _try_parse(raw2)
        if plan is None:
            raise PlanGenerationFailed(
                f"plan was still schema-invalid after one repair attempt: {error2}"
            )

    # plan_id/task_id are Citadel's own identifiers, not the model's to
    # invent correctly -- the model only had to prove it could produce
    # *some* non-empty strings for them (schema conformance); the real ids
    # are assigned here.
    final_plan = plan.model_copy(update={"plan_id": ids.new_id(ids.PLAN), "task_id": task_id})

    return PlanGenerationResult(
        plan=final_plan,
        model_id=model_id,
        routing_reason=routing.routing_reason,
        repaired=repaired,
    )
