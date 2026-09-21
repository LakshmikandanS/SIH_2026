"""citadel_contracts/state_machines.py -- each machine matches this repo's
lifecycle exactly, and every machine is fail-closed on an unknown state."""

from __future__ import annotations

import pytest

from citadel_contracts.state_machines import (
    AGENT,
    APPROVAL,
    ARTIFACT,
    TASK,
    AgentStatus,
    ApprovalState,
    ArtifactStatus,
    IllegalTransition,
    TaskStatus,
)


def test_task_happy_path_end_to_end():
    state = TaskStatus.CREATED
    for step in (
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.COMPLETED,
    ):
        state = TASK.assert_transition(state, step)
    assert state == TaskStatus.COMPLETED
    assert TaskStatus.COMPLETED in TASK.terminal_states


def test_task_revision_required_returns_to_running_not_forward():
    assert TASK.can(TaskStatus.RUNNING, TaskStatus.REVISION_REQUIRED)
    assert TASK.can(TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.REVISION_REQUIRED)
    assert TASK.can(TaskStatus.REVISION_REQUIRED, TaskStatus.RUNNING)
    assert not TASK.can(TaskStatus.REVISION_REQUIRED, TaskStatus.COMPLETED)


def test_task_cannot_skip_planning():
    assert not TASK.can(TaskStatus.CREATED, TaskStatus.RUNNING)
    with pytest.raises(IllegalTransition):
        TASK.assert_transition(TaskStatus.CREATED, TaskStatus.RUNNING)


def test_artifact_is_strictly_linear_with_no_failure_state():
    state = ArtifactStatus.TEMP
    for step in (
        ArtifactStatus.CANDIDATE,
        ArtifactStatus.VERIFIED,
        ArtifactStatus.APPROVED,
        ArtifactStatus.RELEASED,
    ):
        state = ARTIFACT.assert_transition(state, step)
    assert state == ArtifactStatus.RELEASED


def test_released_artifact_is_terminal():
    assert ArtifactStatus.RELEASED in ARTIFACT.terminal_states
    with pytest.raises(IllegalTransition):
        ARTIFACT.assert_transition(ArtifactStatus.RELEASED, ArtifactStatus.APPROVED)


def test_artifact_cannot_skip_verification():
    with pytest.raises(IllegalTransition):
        ARTIFACT.assert_transition(ArtifactStatus.TEMP, ArtifactStatus.APPROVED)


def test_approval_branches_to_approved_or_rejected():
    assert APPROVAL.can(ApprovalState.REVIEW_REQUIRED, ApprovalState.APPROVED)
    assert APPROVAL.can(ApprovalState.REVIEW_REQUIRED, ApprovalState.REJECTED)
    assert ApprovalState.APPROVED in APPROVAL.terminal_states
    assert ApprovalState.REJECTED in APPROVAL.terminal_states


def test_agent_has_exactly_three_terminal_outcomes():
    assert AGENT.can(AgentStatus.RUNNING, AgentStatus.SUCCESS)
    assert AGENT.can(AgentStatus.RUNNING, AgentStatus.FAILED)
    assert AGENT.can(AgentStatus.RUNNING, AgentStatus.MAX_STEPS)
    assert AGENT.terminal_states == frozenset(
        {AgentStatus.SUCCESS, AgentStatus.FAILED, AgentStatus.MAX_STEPS}
    )


def test_an_unknown_current_state_is_never_traversable():
    """Fail closed: a state string the machine has never heard of has no
    outgoing transitions, rather than being treated as permissive."""
    assert TASK.can("SOME_STATE_THAT_DOES_NOT_EXIST", TaskStatus.RUNNING) is False


def test_illegal_transition_names_the_machine_and_both_states():
    with pytest.raises(IllegalTransition) as excinfo:
        TASK.assert_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
    assert excinfo.value.machine == "Task"
    assert excinfo.value.current == TaskStatus.COMPLETED
    assert excinfo.value.requested == TaskStatus.RUNNING
