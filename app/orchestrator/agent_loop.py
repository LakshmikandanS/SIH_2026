"""The agent execution loop (design doc section 5.1, BB-001).

    THINK        -> given the step's action + current evidence, decide the
                    concrete arguments
    ACTION       -> emit exactly one structured action: {"action": str,
                    "arguments": {...}}
    OBSERVATION  -> receive the Tool Gateway's result envelope
                    (success/result or error)
    DECISION     -> CONTINUE (next step) | RETRY (same step, <=1 retry) |
                    TERMINATE

    Termination states: SUCCESS, FAILED, MAX_STEPS (default 6),
    WAITING_FOR_APPROVAL (reached after the last plan step succeeds and an
    artifact is produced). The agent never calls a tool directly -- every
    ACTION is routed through the Tool Gateway, and the agent never sees a
    capability token it wasn't issued for that specific step.

"A plain function inside the Orchestrator process, executed once per plan
step" (section 5.1) -- `run_agent_loop` below is exactly that: no framework,
no class hierarchy, one function that walks `plan.steps` in order. Every
`ACTION` reaches the Tool Gateway through `app.tool_gateway.invoke` and
nothing else -- this module holds no import of `app.rag`, `app.execution`,
or any other tool backend, which is what
`tests/test_orchestration.py::test_every_plan_step_reaches_the_tool_gateway`
(and a grep for those imports) checks.

`AgentStatus.WAITING_FOR_APPROVAL` does not exist (`app/db/state_machines.py`
-- Agent's own machine is RUNNING -> {SUCCESS, FAILED, MAX_STEPS} only).
Section 5.1 lists `WAITING_FOR_APPROVAL` as a *fourth* termination outcome
of "the agent" in the informal sense used there, but it is a *Task*-level
consequence of the Agent reaching SUCCESS with an artifact produced -- the
mapping `app.orchestrator.service.orchestrate` makes, not a state this
module's `AgentLoopResult.outcome` ever holds directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.capability import CapabilityIssueError, issue_for_step
from app.db.state_machines import AgentStatus
from app.observability import EventType, append_event
from app.orchestrator.schemas import PlanModel, PlanStepModel
from app.orchestrator.think import think
from app.orchestrator.working_memory import WorkingMemory, get_or_create, save
from app.policy import Tool
from app.tool_gateway import invoke, task_resource

#: section 5.1's own default.
DEFAULT_MAX_STEPS = 6

#: "RETRY (same step, <=1 retry)" -- one original attempt plus one retry.
MAX_ATTEMPTS_PER_STEP = 2


@dataclass(frozen=True)
class AgentStepTrace:
    step_id: str
    action: str
    attempt: int
    arguments: dict[str, Any]
    envelope: dict[str, Any]


@dataclass(frozen=True)
class AgentLoopResult:
    outcome: str  # AgentStatus.SUCCESS | AgentStatus.FAILED | AgentStatus.MAX_STEPS
    reason: Optional[str]
    steps: list[AgentStepTrace] = field(default_factory=list)
    memory: Optional[WorkingMemory] = None


def _commit_artifact(task_id: str, result: dict[str, Any], memory: WorkingMemory) -> Optional[str]:
    """Create the Artifact row for a successful `generate_report` call, then
    run the Verifier on it synchronously -- design doc section 6.10: "called
    synchronously right after generation". This is a thin delegator to
    `app.artifact.pipeline.create_and_verify_artifact` (step 8's own module);
    it exists here, in OBSERVATION, because "right after generation" means
    right after this Tool Gateway call returns, and that is nowhere else in
    the agent loop.

    Import kept local to avoid a module-level cycle: `app.artifact` is only
    needed by this one branch, and every other THINK/ACTION/OBSERVATION step
    in the loop needs none of it.

    Returns `None` on a verified artifact (section 4 mapping table rows 1-3);
    returns a failure reason string when the Verifier's five checks do not
    all pass (row 6: "Verifier check fails -> task FAILED", BB-038 -- no
    auto-revision loop for a verification failure, distinct from section
    5.3's one bounded *approval-rejection* revision).
    """
    from app.artifact import create_and_verify_artifact

    outcome = create_and_verify_artifact(task_id=task_id, result=result, evidence=memory.evidence)
    memory.artifact_id = outcome.artifact_id
    memory.artifact_path = outcome.artifact_path
    return outcome.failure_reason


def _apply_observation(
    step: PlanStepModel, result: dict[str, Any], memory: WorkingMemory, task_id: str
) -> Optional[str]:
    """OBSERVATION's effect on Working Memory -- what the *next* step's
    THINK reads (`test_observation_feeds_the_next_think`'s own contract).

    Returns `None` normally; returns a failure reason string only for
    `generate_report`, when the Verifier rejects the artifact (section 4
    mapping table row 6) -- the one OBSERVATION outcome that can turn an
    otherwise-successful Tool Gateway call into a loop-level failure.
    """
    if step.action == Tool.RAG_SEARCH:
        memory.evidence = list(result.get("results", []))
    elif step.action == Tool.PYTHON_EXECUTE:
        memory.raw_python_result = dict(result)
        stdout = result.get("stdout") or ""
        try:
            memory.computed = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            memory.computed = None
    elif step.action == Tool.GENERATE_REPORT:
        return _commit_artifact(task_id, result, memory)
    return None


def run_agent_loop(
    *,
    task_id: str,
    agent_id: str,
    plan: PlanModel,
    max_steps: int = DEFAULT_MAX_STEPS,
    revision_comment: Optional[str] = None,
    memory: Optional[WorkingMemory] = None,
) -> AgentLoopResult:
    """Walk `plan.steps` in order, THINK -> ACTION -> OBSERVATION -> DECISION
    each time. Returns once the loop reaches one of its three own outcomes
    (`SUCCESS`, `FAILED`, `MAX_STEPS`) -- `WAITING_FOR_APPROVAL` is the
    caller's (`app.orchestrator.service`) job to derive from `SUCCESS`.

    `revision_comment`, when given, is threaded into THINK for a
    `generate_report` step only -- section 5.3's one scoped case, where a
    single-step "plan" re-runs just that step with the approver's comment
    appended.
    """
    memory = memory if memory is not None else get_or_create(task_id)
    traces: list[AgentStepTrace] = []

    for index, step in enumerate(plan.steps, start=1):
        if index > max_steps:
            return AgentLoopResult(
                outcome=AgentStatus.MAX_STEPS,
                reason=f"plan step {index} ({step.step_id}) exceeds max_steps={max_steps}",
                steps=traces,
                memory=memory,
            )

        attempt = 0
        while True:
            attempt += 1

            # THINK
            arguments = think(step, memory, revision_comment=revision_comment)

            # ACTION -- exactly one structured action, emitted before the
            # Tool Gateway is ever called.
            append_event(
                task_id,
                agent_id,
                EventType.ACTION_REQUESTED,
                {
                    "step_id": step.step_id,
                    "action": step.action,
                    "arguments": arguments,
                    "attempt": attempt,
                },
            )

            # The agent never calls a tool directly: mint the one capability
            # for this step, immediately before it runs, then route through
            # the Tool Gateway -- the only path to any backend.
            try:
                capability = issue_for_step(task_id, agent_id, step.action)
            except CapabilityIssueError as exc:
                return AgentLoopResult(
                    outcome=AgentStatus.FAILED,
                    reason=f"could not issue a capability for step {step.step_id}: {exc}",
                    steps=traces,
                    memory=memory,
                )

            envelope = invoke(
                capability_token=capability.token,
                tool=step.action,
                resource=task_resource(task_id),
                arguments=arguments,
            )
            traces.append(
                AgentStepTrace(
                    step_id=step.step_id,
                    action=step.action,
                    attempt=attempt,
                    arguments=arguments,
                    envelope=envelope,
                )
            )

            # OBSERVATION + DECISION
            if envelope["success"]:
                observation_failure = _apply_observation(step, envelope["result"], memory, task_id)
                save(memory)
                if observation_failure is not None:
                    # Section 4 mapping table row 6: the Verifier rejected
                    # this artifact -- FAILED, no retry, no auto-revision
                    # (BB-038; distinct from section 5.3's approval-rejection
                    # revision, which never reaches this branch).
                    return AgentLoopResult(
                        outcome=AgentStatus.FAILED,
                        reason=observation_failure,
                        steps=traces,
                        memory=memory,
                    )
                break  # CONTINUE to the next plan step

            if attempt < MAX_ATTEMPTS_PER_STEP:
                continue  # RETRY (same step, <=1 retry)

            # TERMINATE
            save(memory)
            error = envelope.get("error") or {}
            return AgentLoopResult(
                outcome=AgentStatus.FAILED,
                reason=(
                    f"step {step.step_id} ({step.action}) failed after "
                    f"{attempt} attempt(s): {error.get('code')} {error.get('message')}"
                ),
                steps=traces,
                memory=memory,
            )

    save(memory)
    return AgentLoopResult(outcome=AgentStatus.SUCCESS, reason=None, steps=traces, memory=memory)
