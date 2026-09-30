"""Control Plane issuance of a capability for one plan step (§6.5).

`issue_capability` in `tokens.py` is the primitive: it signs whatever scope it
is handed. This module is the sanctioned way to *ask* for one, and it derives
the scope from stored state instead of from the caller's argument:

    scope.classification_max = the task's own classification (§3 Task)
    scope.department         = the task owner's department  (§3 User)

That matters because §6.5's example scope
(`{"classification_max": "CONFIDENTIAL", "department": "maintenance"}`) is
exactly the task's classification and the requesting engineer's department. If
a caller could name its own scope, a capability could be minted wider than the
task that justifies it, and the Data Plane's filtering (§6.9) -- which trusts
the scope -- would filter against the wrong envelope.

This is the function the Orchestrator (step 7) calls once per plan step,
immediately before that step runs.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.capability.tokens import CapabilityScope, IssuedCapability, issue_capability
from app.db.engine import SessionLocal
from app.db.models import Agent, Task, User


class CapabilityIssueError(Exception):
    """The requested task/agent pair does not exist or is not paired.

    Not one of §6.6's five error codes, because it is not a tool-call outcome:
    it is a caller bug on the issuing side, raised before any token exists.
    """


def issue_for_step(
    task_id: str,
    agent_id: str,
    operation: str,
    *,
    session: Optional[Session] = None,
    ttl_seconds: Optional[int] = None,
) -> IssuedCapability:
    """Mint the capability for one plan step. Returns token + claims.

    `session` is accepted so a caller already inside a transaction reads its
    own uncommitted Task/Agent rows; issuance itself writes nothing.
    """
    if session is not None:
        return _issue(session, task_id, agent_id, operation, ttl_seconds)
    with SessionLocal() as own:
        return _issue(own, task_id, agent_id, operation, ttl_seconds)


def _issue(
    session: Session,
    task_id: str,
    agent_id: str,
    operation: str,
    ttl_seconds: Optional[int],
) -> IssuedCapability:
    task = session.get(Task, task_id)
    if task is None:
        raise CapabilityIssueError(f"unknown task {task_id!r}")

    agent = session.get(Agent, agent_id)
    if agent is None:
        raise CapabilityIssueError(f"unknown agent {agent_id!r}")
    if agent.task_id != task_id:
        # §3/BB-014: exactly one Agent per task. A capability that paired an
        # agent with someone else's task would be a cross-task grant.
        raise CapabilityIssueError(
            f"agent {agent_id!r} belongs to task {agent.task_id!r}, not {task_id!r}"
        )

    owner = session.get(User, task.user_id)
    if owner is None:
        raise CapabilityIssueError(f"task {task_id!r} has no owning user")

    scope = CapabilityScope(
        classification_max=task.classification,
        department=owner.department,
    )
    return issue_capability(
        task_id=task_id,
        agent_id=agent_id,
        operation=operation,
        scope=scope,
        ttl_seconds=ttl_seconds,
    )
