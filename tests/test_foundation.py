"""Step 3 (foundation-schema) contract tests.

Section 9's five categories all assume components that do not exist yet, so
this module covers foundation-schema's own "Done when" criteria: the six
tables match section 3 field-for-field, and the three state machines of
section 4 are independent and fail closed.
"""

from __future__ import annotations

import pytest

from app.db import init_db
from app.db.models import Agent, Approval, Artifact, Event, Task, User
from app.db.state_machines import (
    AgentStatus,
    ApprovalState,
    ArtifactStatus,
    Classification,
    IllegalTransition,
    TaskStatus,
)
from app.db import state_machines as sm
from app.db.transitions import (
    VersionConflict,
    transition_agent,
    transition_approval,
    transition_artifact,
    transition_task,
)


# --------------------------------------------------------------------------
# Section 3 -- the six tables
# --------------------------------------------------------------------------


def test_exactly_the_six_foundation_tables_exist():
    assert init_db.table_names() == [
        "agents",
        "approvals",
        "artifacts",
        "events",
        "tasks",
        "users",
    ]


@pytest.mark.parametrize(
    "model, required_fields",
    [
        (User, {"user_id", "username", "roles", "clearance", "department"}),
        (
            Task,
            {
                "task_id",
                "user_id",
                "classification",
                "status",
                "requirements",
                "created_at",
            },
        ),
        (Agent, {"agent_id", "task_id", "agent_type", "status"}),
        (
            Artifact,
            {"artifact_id", "task_id", "version", "type", "status", "hash", "provenance"},
        ),
        (
            Approval,
            {"approval_id", "artifact_id", "approver_id", "decision", "comment", "timestamp"},
        ),
        (
            Event,
            {
                "event_id",
                "task_id",
                "actor_id",
                "event_type",
                "payload",
                "timestamp",
                "previous_hash",
                "hash",
            },
        ),
    ],
)
def test_table_carries_every_field_from_the_domain_model(model, required_fields):
    columns = set(model.__table__.columns.keys())
    assert required_fields <= columns, f"missing: {required_fields - columns}"


def test_one_agent_row_per_task_is_a_schema_constraint(db, task):
    """Section 3 / BB-014: exactly one Agent per task, no Supervisor pattern."""
    db.add(Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher"))
    db.commit()

    db.add(Agent(agent_id="A456", task_id=task.task_id, agent_type="writer"))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_approver_id_has_no_default(db, task):
    """It is written once, from the session, at decision time -- never
    defaulted and never taken from a request body (section 6.4)."""
    artifact = Artifact(
        artifact_id="ART123", task_id=task.task_id, type="maintenance_summary_report"
    )
    db.add(artifact)
    db.commit()

    approval = Approval(approval_id="APR123", artifact_id=artifact.artifact_id)
    db.add(approval)
    db.commit()

    assert approval.approver_id is None
    assert approval.decision is None
    assert approval.state == ApprovalState.NOT_REQUIRED


# --------------------------------------------------------------------------
# Section 4 -- three independent state machines
# --------------------------------------------------------------------------


def test_the_three_machines_are_separate_objects():
    assert sm.TASK is not sm.ARTIFACT is not sm.APPROVAL
    assert sm.TASK.states.isdisjoint(sm.ARTIFACT.states)
    # Approval and Artifact share the label APPROVED but not the machine.
    assert sm.APPROVAL.states & sm.ARTIFACT.states == {"APPROVED"}
    assert sm.APPROVAL.can(ApprovalState.REVIEW_REQUIRED, ApprovalState.APPROVED)
    assert not sm.ARTIFACT.can(ArtifactStatus.CANDIDATE, ArtifactStatus.APPROVED)


def test_happy_path_task_sequence(db, task):
    for nxt in (
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.COMPLETED,
    ):
        transition_task(db, task, nxt)
    db.commit()
    assert task.status == TaskStatus.COMPLETED


def test_task_cannot_skip_states(db, task):
    with pytest.raises(IllegalTransition):
        transition_task(db, task, TaskStatus.COMPLETED)


def test_terminal_task_states_are_terminal(db, task):
    transition_task(db, task, TaskStatus.FAILED)
    with pytest.raises(IllegalTransition):
        transition_task(db, task, TaskStatus.RUNNING)


def test_revision_returns_to_running(db, task):
    """Section 5.3: reject -> REVISION_REQUIRED -> RUNNING, one bounded pass."""
    transition_task(db, task, TaskStatus.PLANNING)
    transition_task(db, task, TaskStatus.RUNNING)
    transition_task(db, task, TaskStatus.WAITING_FOR_APPROVAL)
    transition_task(db, task, TaskStatus.REVISION_REQUIRED)
    transition_task(db, task, TaskStatus.RUNNING)
    assert task.status == TaskStatus.RUNNING


def test_artifact_walks_the_linear_path(db, task):
    artifact = Artifact(
        artifact_id="ART123", task_id=task.task_id, type="maintenance_summary_report"
    )
    db.add(artifact)
    db.commit()

    for nxt in (
        ArtifactStatus.CANDIDATE,
        ArtifactStatus.VERIFIED,
        ArtifactStatus.APPROVED,
        ArtifactStatus.RELEASED,
    ):
        transition_artifact(db, artifact, nxt)
    db.commit()
    assert artifact.status == ArtifactStatus.RELEASED


def test_released_artifact_is_terminal(db, task):
    """The state-machine backstop behind section 6.10's API-layer
    immutability check. (The API-layer check itself is step 8.)"""
    artifact = Artifact(
        artifact_id="ART123",
        task_id=task.task_id,
        type="maintenance_summary_report",
        status=ArtifactStatus.RELEASED,
    )
    db.add(artifact)
    db.commit()

    for target in (
        ArtifactStatus.TEMP,
        ArtifactStatus.CANDIDATE,
        ArtifactStatus.VERIFIED,
        ArtifactStatus.APPROVED,
    ):
        with pytest.raises(IllegalTransition):
            transition_artifact(db, artifact, target)


def test_approval_decision_mirrors_its_terminal_state(db, task):
    artifact = Artifact(
        artifact_id="ART123", task_id=task.task_id, type="maintenance_summary_report"
    )
    db.add(artifact)
    db.commit()
    approval = Approval(approval_id="APR123", artifact_id=artifact.artifact_id)
    db.add(approval)
    db.commit()

    transition_approval(db, approval, ApprovalState.REVIEW_REQUIRED)
    assert approval.decision is None

    transition_approval(db, approval, ApprovalState.APPROVED)
    assert approval.decision == ApprovalState.APPROVED


def test_agent_terminates_in_one_of_the_four_states(db, task):
    for status in (AgentStatus.SUCCESS, AgentStatus.FAILED, AgentStatus.MAX_STEPS):
        agent = Agent(agent_id=f"A-{status}", task_id=task.task_id, agent_type="researcher")
        assert sm.AGENT.can(AgentStatus.RUNNING, status)
    assert sm.AGENT.terminal_states == {
        AgentStatus.SUCCESS,
        AgentStatus.FAILED,
        AgentStatus.MAX_STEPS,
    }


def test_unknown_states_are_never_traversable():
    """Fail closed (AGENTS.md section 9): an unmatched state is a DENY, not a
    pass."""
    assert not sm.TASK.can("RUNNING", "QUARANTINED")
    assert not sm.TASK.can("NOT_A_STATE", "RUNNING")
    assert not sm.ARTIFACT.can(ArtifactStatus.TEMP, "SHIPPED")


# --------------------------------------------------------------------------
# Section 6.11 -- optimistic versioning
# --------------------------------------------------------------------------


def test_version_increments_on_each_transition(db, task):
    assert task.version == 1
    transition_task(db, task, TaskStatus.PLANNING, expected_version=1)
    assert task.version == 2
    transition_task(db, task, TaskStatus.RUNNING, expected_version=2)
    assert task.version == 3


def test_stale_expected_version_conflicts(db, task):
    transition_task(db, task, TaskStatus.PLANNING, expected_version=1)
    with pytest.raises(VersionConflict) as exc:
        transition_task(db, task, TaskStatus.RUNNING, expected_version=1)
    assert exc.value.expected == 1
    assert exc.value.actual == 2


def test_conflict_leaves_state_untouched(db, task):
    with pytest.raises(VersionConflict):
        transition_task(db, task, TaskStatus.PLANNING, expected_version=99)
    assert task.status == TaskStatus.CREATED
    assert task.version == 1


# --------------------------------------------------------------------------
# Classification lattice (needed by the Policy Engine and the Verifier)
# --------------------------------------------------------------------------


def test_classification_ordering():
    assert Classification.exceeds("CONFIDENTIAL", "INTERNAL")
    assert not Classification.exceeds("INTERNAL", "CONFIDENTIAL")
    assert not Classification.exceeds("CONFIDENTIAL", "CONFIDENTIAL")


def test_unknown_classification_fails_closed():
    with pytest.raises(ValueError):
        Classification.exceeds("COSMIC_TOP_SECRET", "CONFIDENTIAL")
