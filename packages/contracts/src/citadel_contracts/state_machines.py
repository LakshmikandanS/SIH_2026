"""The three lifecycle state machines, plus the Agent lifecycle.

Ported unchanged from the prototype's `contracts/state_machines.py`
(`packages/contracts/AGENTS.md` porting table: no change on the way in),
except that `Classification` now lives in its own file,
`citadel_contracts/classification.py` -- see that module's docstring for why.

Kept as three *independent* machines, never merged into one status enum.
The correspondence between them is a join performed by the orchestrator and
the approval endpoint (`citadel_runtime`), not a shared column -- this
module does not introduce a fourth object to hold it.

Each machine is pure data (a transition table). Adding a state later -- a
crash-recovery / RESUMABLE state, say -- is an edit to the dict below and
nothing else: no component reads these strings directly, they call
`assert_transition`.
"""

from __future__ import annotations

from typing import Mapping, Set


class IllegalTransition(Exception):
    """Raised when a component attempts a transition the contract forbids."""

    def __init__(self, machine: str, current: str, requested: str) -> None:
        super().__init__(
            f"{machine}: illegal transition {current!r} -> {requested!r}"
        )
        self.machine = machine
        self.current = current
        self.requested = requested


class StateMachine:
    """A named, data-driven, fail-closed state machine."""

    def __init__(self, name: str, transitions: Mapping[str, Set[str]], initial: str) -> None:
        self.name = name
        self._transitions = {k: frozenset(v) for k, v in transitions.items()}
        self.initial = initial

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self._transitions)

    @property
    def terminal_states(self) -> frozenset[str]:
        return frozenset(s for s, nxt in self._transitions.items() if not nxt)

    def is_state(self, state: str) -> bool:
        return state in self._transitions

    def can(self, current: str, requested: str) -> bool:
        # Unknown states are never traversable -- fail closed.
        return requested in self._transitions.get(current, frozenset())

    def assert_transition(self, current: str, requested: str) -> str:
        if not self.can(current, requested):
            raise IllegalTransition(self.name, current, requested)
        return requested


# --- Task ----------------------------------------------------------------
# CREATED -> PLANNING -> RUNNING -> WAITING_FOR_APPROVAL -> COMPLETED
#                           |
#                         FAILED
# REVISION_REQUIRED is a transient sub-state of RUNNING, reachable from
# RUNNING and from WAITING_FOR_APPROVAL (an approver REJECT), and it always
# returns to RUNNING for one bounded revision.
class TaskStatus:
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TASK = StateMachine(
    "Task",
    {
        TaskStatus.CREATED: {TaskStatus.PLANNING, TaskStatus.FAILED},
        TaskStatus.PLANNING: {TaskStatus.RUNNING, TaskStatus.FAILED},
        TaskStatus.RUNNING: {
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.REVISION_REQUIRED,
            TaskStatus.FAILED,
        },
        TaskStatus.REVISION_REQUIRED: {TaskStatus.RUNNING, TaskStatus.FAILED},
        TaskStatus.WAITING_FOR_APPROVAL: {
            TaskStatus.COMPLETED,
            TaskStatus.REVISION_REQUIRED,
            TaskStatus.FAILED,
        },
        TaskStatus.COMPLETED: set(),
        TaskStatus.FAILED: set(),
    },
    initial=TaskStatus.CREATED,
)


# --- Artifact --------------------------------------------------------------
# TEMP -> CANDIDATE -> VERIFIED -> APPROVED -> RELEASED
# Strictly linear. There is no artifact failure state: a failed verification
# leaves the artifact at TEMP and fails the *task* instead. RELEASED is
# terminal -- that terminality is what release-immutability depends on.
class ArtifactStatus:
    TEMP = "TEMP"
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"


ARTIFACT = StateMachine(
    "Artifact",
    {
        ArtifactStatus.TEMP: {ArtifactStatus.CANDIDATE},
        ArtifactStatus.CANDIDATE: {ArtifactStatus.VERIFIED},
        ArtifactStatus.VERIFIED: {ArtifactStatus.APPROVED},
        ArtifactStatus.APPROVED: {ArtifactStatus.RELEASED},
        ArtifactStatus.RELEASED: set(),
    },
    initial=ArtifactStatus.TEMP,
)


# --- Approval ---------------------------------------------------------------
# NOT_REQUIRED -> REVIEW_REQUIRED -> APPROVED / REJECTED
class ApprovalState:
    NOT_REQUIRED = "NOT_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


APPROVAL = StateMachine(
    "Approval",
    {
        ApprovalState.NOT_REQUIRED: {ApprovalState.REVIEW_REQUIRED},
        ApprovalState.REVIEW_REQUIRED: {ApprovalState.APPROVED, ApprovalState.REJECTED},
        ApprovalState.APPROVED: set(),
        ApprovalState.REJECTED: set(),
    },
    initial=ApprovalState.NOT_REQUIRED,
)


# --- Agent lifecycle ---------------------------------------------------------
# Not one of the three machines above, but the Agent row carries a status
# enum, and the agent loop's four termination states map onto it.
class AgentStatus:
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    MAX_STEPS = "MAX_STEPS"


AGENT = StateMachine(
    "Agent",
    {
        AgentStatus.RUNNING: {
            AgentStatus.SUCCESS,
            AgentStatus.FAILED,
            AgentStatus.MAX_STEPS,
        },
        AgentStatus.SUCCESS: set(),
        AgentStatus.FAILED: set(),
        AgentStatus.MAX_STEPS: set(),
    },
    initial=AgentStatus.RUNNING,
)


class AgentType:
    """Informational label only -- selects a prompt/tool-permission profile.
    It does not spawn a process or a second Agent identity."""

    RESEARCHER = "researcher"
    WRITER = "writer"


class Role:
    ENGINEER = "engineer"
    APPROVER = "approver"
    ADMIN = "admin"


__all__ = [
    "IllegalTransition",
    "StateMachine",
    "TaskStatus",
    "TASK",
    "ArtifactStatus",
    "ARTIFACT",
    "ApprovalState",
    "APPROVAL",
    "AgentStatus",
    "AGENT",
    "AgentType",
    "Role",
]
