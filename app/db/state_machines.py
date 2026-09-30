"""The three state machines of design doc §4, plus the Agent lifecycle.

They are kept as three *independent* machines and are never merged into one
status enum. The correspondence between them (§4's mapping table) is a join
performed by the Orchestrator and the approval endpoint, not a shared column.

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
        # Unknown states are never traversable -- fail closed (AGENTS.md §9).
        return requested in self._transitions.get(current, frozenset())

    def assert_transition(self, current: str, requested: str) -> str:
        if not self.can(current, requested):
            raise IllegalTransition(self.name, current, requested)
        return requested


# --- Task (§4) ----------------------------------------------------------------
# CREATED -> PLANNING -> RUNNING -> WAITING_FOR_APPROVAL -> COMPLETED
#                           |
#                         FAILED
# REVISION_REQUIRED is a transient sub-state of RUNNING (§4 note), reachable
# from RUNNING (§4 mapping table) and from WAITING_FOR_APPROVAL (§6.10, an
# approver REJECT), and it always returns to RUNNING for the one bounded
# revision of §5.3.
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


# --- Artifact (§4) ------------------------------------------------------------
# TEMP -> CANDIDATE -> VERIFIED -> APPROVED -> RELEASED
# Strictly linear. There is no artifact failure state: a failed verification
# leaves the artifact at TEMP and fails the *task* (§4 mapping table, BB-038).
# RELEASED is terminal -- that terminality is what §6.10's API-layer
# immutability check enforces.
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


# --- Approval (§4) ------------------------------------------------------------
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


# --- Agent lifecycle (§3) -----------------------------------------------------
# Not one of the three §4 machines, but the Agent row carries a status enum
# whose values §3 fixes, and the agent loop's four termination states (§5.1)
# map onto it.
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
    It does NOT spawn a process or a second Agent identity (§3, BB-014)."""

    RESEARCHER = "researcher"
    WRITER = "writer"


class Role:
    ENGINEER = "engineer"
    APPROVER = "approver"
    ADMIN = "admin"


class Classification:
    """Ordered classification lattice. Comparison is needed by the Policy
    Engine (§6.7 `resource.classification > task.classification`) and the
    Verifier (§6.10); it is defined once, here."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"

    _ORDER = {PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2}

    @classmethod
    def rank(cls, level: str) -> int:
        try:
            return cls._ORDER[level]
        except KeyError:  # fail closed on an unknown marking
            raise ValueError(f"unknown classification {level!r}") from None

    @classmethod
    def exceeds(cls, resource_level: str, task_level: str) -> bool:
        """True when `resource_level` is above `task_level` (a DENY in §6.7)."""
        return cls.rank(resource_level) > cls.rank(task_level)
