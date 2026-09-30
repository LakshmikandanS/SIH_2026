"""The Orchestrator's own entry point (design doc section 6.2, section 5).

    "Synchronous `POST /internal/orchestrate` with the canonical handoff
     payload [...] Orchestrator now owns task execution and status."

`orchestrate()` is the one function both `POST /task` (the Query Router
role) and `POST /internal/orchestrate` call -- see `app/orchestrator/router.py`
for why the second is still its own real endpoint even though the first
calls this in-process rather than over HTTP (section 2: both roles share one
trusted-zone process).

The whole walk, in order: validate the handoff -> create the one Agent row
(BB-014: exactly one per task) -> PLANNING -> generate the plan (section 5.2)
-> RUNNING -> the agent loop (section 5.1) -> WAITING_FOR_APPROVAL on success
with an artifact, FAILED on anything else. Every Task-status change goes
through `app.orchestrator.state.commit_task_transition`, never a bare
`session.commit()` on `task.status` -- that is what keeps section 6.11's
`expected_version` check and the `STATE_COMMITTED` event from being skipped
at any one of these several call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app import ids
from app.db.engine import SessionLocal
from app.db.models import Agent, Task
from app.db.state_machines import AgentStatus, AgentType, TaskStatus
from app.db.transitions import transition_agent, transition_task
from app.model_router import ModelRoutingError
from app.observability import EventType, append_event
from app.orchestrator.agent_loop import DEFAULT_MAX_STEPS, run_agent_loop
from app.orchestrator.errors import INVALID_REQUIREMENTS, TASK_ALREADY_RUNNING, OrchestrationError
from app.orchestrator.plan import PlanGenerationFailed, generate_plan
from app.orchestrator.schemas import OrchestratePayload
from app.orchestrator.state import commit_task_transition

#: section 6.2's canonical handoff names both flags; this slice's one fixed
#: scenario needs both to be true (rag.search AND generate_report are both
#: plan steps) -- anything else is `INVALID_REQUIREMENTS`, since there is no
#: general planner in this MVP that could serve a different requirements
#: combination (section 5.2).
_REQUIRED_REQUIREMENT_KEYS = ("needs_rag", "needs_document_generation")


@dataclass(frozen=True)
class OrchestrateResult:
    task_id: str
    status: str
    agent_status: Optional[str]
    artifact_id: Optional[str]
    reason: Optional[str]


def _validate_requirements(requirements: dict) -> None:
    for key in _REQUIRED_REQUIREMENT_KEYS:
        if requirements.get(key) is not True:
            raise OrchestrationError(
                INVALID_REQUIREMENTS,
                f"this slice's fixed plan requires {_REQUIRED_REQUIREMENT_KEYS} "
                f"all true; got {requirements!r}",
            )


def _start_agent(payload: OrchestratePayload) -> str:
    """Validate the handoff, create the one Agent row, move Task to
    PLANNING. Returns the new `agent_id`. Raises `OrchestrationError` for
    section 6.2's two named failure codes."""
    with SessionLocal() as session:
        task = session.get(Task, payload.task_id)
        if task is None:
            raise OrchestrationError(
                INVALID_REQUIREMENTS, f"unknown task {payload.task_id!r}"
            )
        if task.status != TaskStatus.CREATED:
            raise OrchestrationError(
                TASK_ALREADY_RUNNING,
                f"task {payload.task_id!r} is already {task.status!r}",
            )
        existing_agent = session.execute(
            select(Agent).where(Agent.task_id == task.task_id)
        ).scalar_one_or_none()
        if existing_agent is not None:
            raise OrchestrationError(
                TASK_ALREADY_RUNNING,
                f"task {payload.task_id!r} already has an agent (BB-014: exactly one per task)",
            )

        _validate_requirements(payload.requirements)

        agent_id = ids.new_id(ids.AGENT)
        agent = Agent(
            agent_id=agent_id,
            task_id=task.task_id,
            agent_type=AgentType.RESEARCHER,
            status=AgentStatus.RUNNING,
        )
        session.add(agent)
        transition_task(session, task, TaskStatus.PLANNING, expected_version=task.version)
        session.flush()
        append_event(
            task.task_id,
            agent_id,
            EventType.STATE_COMMITTED,
            {"entity": "task", "new_status": TaskStatus.PLANNING, "version": task.version},
            session=session,
        )
        session.commit()

    append_event(
        payload.task_id,
        agent_id,
        EventType.AGENT_STARTED,
        {"agent_id": agent_id, "agent_type": AgentType.RESEARCHER},
    )
    return agent_id


def _fail_task(task_id: str, agent_id: Optional[str], reason: str) -> None:
    """Mark the Agent row (if one exists) FAILED, then the Task FAILED --
    used for every failure path before the agent loop itself has a chance
    to report its own outcome (routing failure, plan generation failure)."""
    if agent_id is not None:
        with SessionLocal() as session:
            agent = session.get(Agent, agent_id)
            if agent is not None and agent.status == AgentStatus.RUNNING:
                transition_agent(session, agent, AgentStatus.FAILED)
                append_event(
                    task_id,
                    agent_id,
                    EventType.STATE_COMMITTED,
                    {"entity": "agent", "new_status": AgentStatus.FAILED, "reason": reason},
                    session=session,
                )
                session.commit()
    commit_task_transition(task_id, TaskStatus.FAILED)


def orchestrate(
    payload: OrchestratePayload, *, max_steps: int = DEFAULT_MAX_STEPS
) -> OrchestrateResult:
    """section 6.2's canonical handoff, run synchronously to completion.

    Raises `OrchestrationError` only for the two section 6.2 codes
    (`TASK_ALREADY_RUNNING`, `INVALID_REQUIREMENTS`) -- these are the
    handoff being malformed or repeated, not a task-execution failure. Every
    other failure (model routing, plan generation, the agent loop itself)
    is reported as a normal `OrchestrateResult` with `status=FAILED`, because
    the Task row *was* successfully created and started; it simply did not
    finish successfully. This mirrors section 6.7's handling of "denied is a
    first-class outcome, not an exception" one layer up.
    """
    agent_id = _start_agent(payload)

    try:
        plan_result = generate_plan(payload.task_id, payload.classification)
    except (PlanGenerationFailed, ModelRoutingError) as exc:
        reason = str(exc)
        _fail_task(payload.task_id, agent_id, reason)
        return OrchestrateResult(
            task_id=payload.task_id,
            status=TaskStatus.FAILED,
            agent_status=AgentStatus.FAILED,
            artifact_id=None,
            reason=reason,
        )

    plan = plan_result.plan
    append_event(
        payload.task_id,
        agent_id,
        EventType.PLAN_CREATED,
        {
            "plan_id": plan.plan_id,
            "model_id": plan_result.model_id,
            "routing_reason": plan_result.routing_reason,
            "repaired": plan_result.repaired,
            "steps": [step.model_dump() for step in plan.steps],
        },
    )

    commit_task_transition(payload.task_id, TaskStatus.RUNNING)

    loop_result = run_agent_loop(
        task_id=payload.task_id, agent_id=agent_id, plan=plan, max_steps=max_steps
    )

    with SessionLocal() as session:
        agent = session.get(Agent, agent_id)
        transition_agent(session, agent, loop_result.outcome)
        append_event(
            payload.task_id,
            agent_id,
            EventType.STATE_COMMITTED,
            {"entity": "agent", "new_status": loop_result.outcome, "reason": loop_result.reason},
            session=session,
        )
        session.commit()

    if loop_result.outcome == AgentStatus.SUCCESS:
        commit_task_transition(payload.task_id, TaskStatus.WAITING_FOR_APPROVAL)
        artifact_id = loop_result.memory.artifact_id if loop_result.memory else None
        return OrchestrateResult(
            task_id=payload.task_id,
            status=TaskStatus.WAITING_FOR_APPROVAL,
            agent_status=AgentStatus.SUCCESS,
            artifact_id=artifact_id,
            reason=None,
        )

    # FAILED and MAX_STEPS both end the Task FAILED (§4's Task machine has no
    # MAX_STEPS state of its own -- see app/orchestrator/agent_loop.py).
    commit_task_transition(payload.task_id, TaskStatus.FAILED)
    return OrchestrateResult(
        task_id=payload.task_id,
        status=TaskStatus.FAILED,
        agent_status=loop_result.outcome,
        artifact_id=None,
        reason=loop_result.reason,
    )
