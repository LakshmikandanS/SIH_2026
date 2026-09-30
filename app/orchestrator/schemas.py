"""The Plan schema (design doc section 5.2) and the two orchestration
payloads (section 6.2's canonical handoff, and `/task`'s own request body).

`PlanModel`/`PlanStepModel` are pydantic models -- this codebase's existing
convention throughout `app/` -- used both to validate the reasoning model's
structured-output call (`PlanModel.model_validate`) and to produce the actual
JSON Schema a repair prompt quotes back at the model
(`PlanModel.model_json_schema()`). No `jsonschema` dependency is added: a
pydantic model *is* a JSON Schema plus a validator for it, and this codebase
already depends on pydantic for every other request/response shape.

Action names are read from `app.policy.tools.Tool`, never written as a
literal here -- `tests/test_security.py::test_no_code_path_invokes_a_tool_outside_the_gateway`
greps for exactly that.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.policy import Tool

#: The three tool names this slice's fixed plan shape may use, and the exact
#: order design doc section 5.2 specifies. Not a general planning algorithm:
#: "the plan shape for this scenario is effectively fixed" (section 5.2).
ALLOWED_ACTIONS: tuple[str, ...] = (Tool.RAG_SEARCH, Tool.PYTHON_EXECUTE, Tool.GENERATE_REPORT)
EXPECTED_ACTION_SEQUENCE: tuple[str, ...] = ALLOWED_ACTIONS

#: The one argument key each step's action requires the *planner* to have
#: attempted to fill in, checked at schema-validation time so a plan missing
#: it triggers the one repair prompt rather than surfacing later as a THINK
#: failure. `python.execute`'s `code` is intentionally still required here
#: even though `app.orchestrator.think` never executes the planner's
#: version of it (see that module's docstring) -- the key must still be
#: *present* for the plan to be schema-conformant against section 5.2's own
#: example shape.
_REQUIRED_ARGUMENT_KEYS: dict[str, str] = {
    Tool.RAG_SEARCH: "query",
    Tool.PYTHON_EXECUTE: "code",
    Tool.GENERATE_REPORT: "template",
}


class PlanStepModel(BaseModel):
    step_id: str = Field(min_length=1)
    agent_type: str = Field(min_length=1)
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def _action_is_one_of_the_three_tools(cls, value: str) -> str:
        if value not in ALLOWED_ACTIONS:
            raise ValueError(
                f"action {value!r} is not one of this slice's three allowed "
                f"tools {ALLOWED_ACTIONS}"
            )
        return value

    @model_validator(mode="after")
    def _has_its_required_argument(self) -> "PlanStepModel":
        required_key = _REQUIRED_ARGUMENT_KEYS.get(self.action)
        if required_key is not None:
            value = self.arguments.get(required_key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"step {self.step_id!r} (action {self.action!r}) is missing "
                    f"a non-empty {required_key!r} argument"
                )
        return self


class PlanModel(BaseModel):
    """A generic, validated sequence of steps -- deliberately not itself
    constrained to the fixed three-step shape below. `PlanModel` is reused
    for two different things: the reasoning model's own plan output (which
    `app.orchestrator.plan.validate_fixed_shape` additionally checks matches
    section 5.2's exact shape) and section 5.3's synthetic one-step revision
    "plan" (`app.orchestrator.revision`, which deliberately does NOT match
    that shape -- it names one step, `generate_report`, on purpose)."""

    plan_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    steps: list[PlanStepModel]


def validate_fixed_shape(plan: PlanModel) -> None:
    """section 5.2's own constraint on top of the generic schema above: THIS
    scenario's plan is always exactly these three steps, in this order.
    Raises `ValueError` (caught by `app.orchestrator.plan` as a
    schema-validation failure, triggering the one repair prompt) otherwise.
    Only ever called against the reasoning model's own output -- never
    against section 5.3's synthetic revision plan.
    """
    actions = tuple(step.action for step in plan.steps)
    if actions != EXPECTED_ACTION_SEQUENCE:
        raise ValueError(
            "design doc section 5.2: this scenario's plan shape is fixed "
            f"-- expected the three steps {EXPECTED_ACTION_SEQUENCE} in "
            f"that exact order, got {actions}"
        )


class OrchestratePayload(BaseModel):
    """section 6.2's canonical handoff JSON, exactly."""

    task_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    classification: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    requirements: dict[str, Any] = Field(default_factory=dict)


class TaskCreateRequest(BaseModel):
    """`/task`'s own request body -- the CLI's `/task "<text>" --classification
    <level>` (section 1.1 step 2), before the Query Router's rule-based
    classification step (section 6.2's own wording: the slash command
    implies class TASK, and `--classification` is the user-declared,
    authoritative field, never inferred)."""

    text: str = Field(min_length=1)
    classification: str = Field(min_length=1)
