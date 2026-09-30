"""Plan revision (design doc section 5.3, BB-003) -- scoped to exactly one case.

    "Triggered only when an approver REJECTs a VERIFIED artifact:
     1. Keep all completed steps' evidence and events untouched.
     2. Discard only the generate_report step's output artifact.
     3. Re-run generate_report once with the approver's comment appended to
        the prompt.
     4. If the second attempt is also rejected, the task ends FAILED -- no
        further revision loop."

`revise_report` is the whole of this module. There is no general
replanning, no step re-ordering, and no mid-execution plan change for any
other trigger -- this function does not accept a trigger reason, does not
look at *why* it was called, and only ever re-runs one step
(`generate_report`). The caller (step 8's approval-decision endpoint, which
this step does not build) is responsible for calling this only when an
approver has actually rejected a `VERIFIED` artifact; this function's own
job is to make sure that even if it is called, it enforces "exactly once"
itself rather than trusting the caller to count.

Rule 1 -- "keep all completed steps' evidence and events untouched" -- is
why this module never re-runs `rag.search` or `python.execute`: it reads the
same `WorkingMemory` the original run populated
(`app.orchestrator.working_memory`, see that module's docstring for the one
narrow exception to "Working Memory is not persisted" this requires) and
only issues a fresh capability for `generate_report` itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select

from app.db.engine import SessionLocal
from app.db.models import Artifact, Task
from app.db.state_machines import AgentStatus, TaskStatus
from app.orchestrator.agent_loop import run_agent_loop
from app.orchestrator.schemas import PlanModel, PlanStepModel
from app.orchestrator.state import commit_task_transition
from app.orchestrator.working_memory import load as load_memory
from app.policy import Tool

#: One artifact already exists (the original, rejected run). A second
#: existing artifact means this task has already been through the one
#: revision section 5.3 allows -- calling this a third time is refused.
_MAX_REPORT_ARTIFACTS_BEFORE_REVISION = 1


class RevisionNotPermitted(Exception):
    """Raised when `revise_report` is called for a task that has already
    used its one revision (section 5.3 rule 4)."""


@dataclass(frozen=True)
class RevisionOutcome:
    task_id: str
    status: str  # TaskStatus.WAITING_FOR_APPROVAL | TaskStatus.FAILED
    artifact_id: Optional[str]
    reason: Optional[str]


def _report_artifact_count(task_id: str) -> int:
    with SessionLocal() as session:
        return session.execute(
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.task_id == task_id, Artifact.type == "maintenance_summary_report")
        ).scalar_one()


def _single_step_plan(task_id: str, plan_id: str) -> PlanModel:
    """A one-step "plan" containing only `generate_report`, re-validated
    through the same `PlanModel` schema the original plan used -- section
    5.3 re-runs exactly one step, not the whole plan."""
    return PlanModel(
        plan_id=plan_id,
        task_id=task_id,
        steps=[
            PlanStepModel(
                step_id="S3-REVISION",
                agent_type="writer",
                action=Tool.GENERATE_REPORT,
                arguments={"template": "maintenance_summary_v1"},
            )
        ],
    )


def revise_report(task_id: str, agent_id: str, comment: str) -> RevisionOutcome:
    """section 5.3, end to end. Call once per rejection.

    Raises `RevisionNotPermitted` if this task has already had its one
    revision -- the caller (step 8's approval endpoint) should treat that as
    "the second attempt was also rejected" and end the task `FAILED` without
    calling this again; this function still fails the task itself so a
    caller that ignores the exception cannot leave the task stuck.
    """
    if _report_artifact_count(task_id) > _MAX_REPORT_ARTIFACTS_BEFORE_REVISION:
        commit_task_transition(task_id, TaskStatus.FAILED)
        raise RevisionNotPermitted(
            f"task {task_id!r} has already used its one section 5.3 revision; "
            f"a second rejection ends the task FAILED"
        )

    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise LookupError(f"unknown task {task_id!r}")
        current_status = task.status
        session.expunge(task)
    if current_status not in (TaskStatus.WAITING_FOR_APPROVAL,):
        raise ValueError(
            f"task {task_id!r} is {current_status!r}; revision only runs against a "
            f"task waiting for approval (section 4's mapping table)"
        )

    memory = load_memory(task_id)
    if memory is None:
        # Rule 1 forbids re-fetching evidence -- if Working Memory is gone
        # (a process restart, most likely; see that module's docstring),
        # there is nothing left to revise from. Fail rather than silently
        # re-running rag.search/python.execute, which section 5.3 forbids.
        commit_task_transition(task_id, TaskStatus.FAILED)
        return RevisionOutcome(
            task_id=task_id,
            status=TaskStatus.FAILED,
            artifact_id=None,
            reason="working memory for this task is no longer available in-process; "
            "section 5.3 does not permit re-running the retrieval or compute steps to "
            "rebuild it",
        )

    # `commit_task_transition` (app.orchestrator.state) already emits its own
    # STATE_COMMITTED event per transition -- no separate one is added here.
    commit_task_transition(task_id, TaskStatus.REVISION_REQUIRED)
    commit_task_transition(task_id, TaskStatus.RUNNING)

    revision_plan = _single_step_plan(task_id, plan_id="P-REVISION")
    loop_result = run_agent_loop(
        task_id=task_id,
        agent_id=agent_id,
        plan=revision_plan,
        max_steps=1,
        revision_comment=comment,
        memory=memory,
    )

    if loop_result.outcome == AgentStatus.SUCCESS:
        commit_task_transition(task_id, TaskStatus.WAITING_FOR_APPROVAL)
        artifact_id = loop_result.memory.artifact_id if loop_result.memory else None
        return RevisionOutcome(
            task_id=task_id, status=TaskStatus.WAITING_FOR_APPROVAL, artifact_id=artifact_id, reason=None
        )

    commit_task_transition(task_id, TaskStatus.FAILED)
    return RevisionOutcome(
        task_id=task_id, status=TaskStatus.FAILED, artifact_id=None, reason=loop_result.reason
    )
