"""Guarded state transitions.

Later steps change Task/Artifact/Approval/Agent state constantly. Routing all
of it through these four functions means the section 4 state machines are
enforced in one place rather than re-checked (or forgotten) per call site, and
that section 6.11's optimistic version check cannot be skipped by accident.

These functions never commit. The caller owns the transaction -- section
6.10's approval decision needs three of these plus two events to land in a
single commit.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.db import state_machines as sm
from app.db.models import Agent, Approval, Artifact, Task


class VersionConflict(Exception):
    """Optimistic-lock failure (section 6.11). Surfaces as 409 CONFLICT.

    In this slice one task runs at a time with a single sequential agent loop,
    so this means "retry the read-modify-write once" -- never a semantic merge
    of concurrent agent outputs (BB-011).
    """

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"task {task_id}: expected version {expected}, found {actual}"
        )
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


def transition_task(
    session: Session,
    task: Task,
    new_status: str,
    expected_version: Optional[int] = None,
) -> Task:
    """Move a Task and bump its version.

    `expected_version` is optional only so that callers holding the row inside
    one transaction need not thread it through; when supplied it is enforced.
    """
    if expected_version is not None and task.version != expected_version:
        raise VersionConflict(task.task_id, expected_version, task.version)

    sm.TASK.assert_transition(task.status, new_status)
    task.status = new_status
    task.version += 1
    session.add(task)
    return task


def transition_artifact(session: Session, artifact: Artifact, new_status: str) -> Artifact:
    """Move an Artifact along TEMP -> CANDIDATE -> VERIFIED -> APPROVED ->
    RELEASED. RELEASED is terminal in the machine, so this raises
    IllegalTransition on any attempt to move a released artifact -- the
    backstop behind section 6.10's API-layer immutability check."""
    sm.ARTIFACT.assert_transition(artifact.status, new_status)
    artifact.status = new_status
    session.add(artifact)
    return artifact


def transition_approval(session: Session, approval: Approval, new_state: str) -> Approval:
    """Move an Approval. The terminal states also set `decision`, keeping the
    section 4 state column and the section 3 domain field in agreement."""
    sm.APPROVAL.assert_transition(approval.state, new_state)
    approval.state = new_state
    if new_state in (sm.ApprovalState.APPROVED, sm.ApprovalState.REJECTED):
        approval.decision = new_state
    session.add(approval)
    return approval


def transition_agent(session: Session, agent: Agent, new_status: str) -> Agent:
    sm.AGENT.assert_transition(agent.status, new_status)
    agent.status = new_status
    session.add(agent)
    return agent
