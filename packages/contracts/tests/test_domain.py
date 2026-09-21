"""citadel_contracts/domain.py -- the plain schemas hold exactly the fields
this repo names, and each is a frozen (immutable) value object."""

from __future__ import annotations

import dataclasses

import pytest

from citadel_contracts.domain import Agent, Approval, Artifact, Evidence, Resource, Task, User


def test_user_carries_no_password_hash():
    """The one deliberate omission: a User crossing this boundary never
    carries the secret."""
    fields = {f.name for f in dataclasses.fields(User)}
    assert "password_hash" not in fields
    assert fields == {"user_id", "username", "roles", "clearance", "department", "created_at"}


def test_task_carries_no_department_field():
    """Department is an attribute of the owning User, not the Task."""
    fields = {f.name for f in dataclasses.fields(Task)}
    assert "department" not in fields


def test_domain_objects_are_frozen():
    user = User(user_id="U123", username="j.rao")
    with pytest.raises(dataclasses.FrozenInstanceError):
        user.username = "someone.else"  # type: ignore[misc]


def test_agent_round_trips_the_shape():
    agent = Agent(agent_id="A123", task_id="T123", agent_type="researcher", status="RUNNING")
    assert agent.agent_id == "A123"
    assert agent.task_id == "T123"


def test_two_agents_may_share_a_task_id_this_shape_does_not_forbid_it():
    """The prototype pinned exactly one agent per task via a
    `uq_agents_one_per_task` ORM constraint. This repo's own root AGENTS.md
    names that pattern as a failure mode by example, so nothing here
    enforces it -- documented as a test, not just a docstring, so the
    absence of the constraint cannot regress silently."""
    researcher = Agent(agent_id="A1", task_id="T123", agent_type="researcher", status="RUNNING")
    writer = Agent(agent_id="A2", task_id="T123", agent_type="writer", status="RUNNING")
    assert researcher.task_id == writer.task_id
    assert researcher != writer


def test_artifact_defaults_match_the_initial_state_machine_state():
    from citadel_contracts.state_machines import ArtifactStatus

    artifact = Artifact(
        artifact_id="ART123", task_id="T123", version=1, type="report", status=ArtifactStatus.TEMP
    )
    assert artifact.status == ArtifactStatus.TEMP
    assert artifact.hash is None
    assert artifact.provenance == ()


def test_approval_state_and_decision_are_independent_fields():
    approval = Approval(approval_id="APR123", artifact_id="ART123")
    assert approval.state == "NOT_REQUIRED"
    assert approval.decision is None


def test_evidence_to_dict_is_the_wire_shape():
    evidence = Evidence(
        evidence_id="E001",
        document_id="DOC-P101-HIST",
        document_version="1",
        page=4,
        text="...",
        classification="CONFIDENTIAL",
        acl=("maintenance", "engineering"),
        provenance_id="E001",
    )
    payload = evidence.to_dict()
    assert payload == {
        "evidence_id": "E001",
        "document_id": "DOC-P101-HIST",
        "document_version": "1",
        "page": 4,
        "text": "...",
        "classification": "CONFIDENTIAL",
        "acl": ["maintenance", "engineering"],
        "provenance_id": "E001",
    }
    assert "score" not in payload, "score is omitted from to_dict() when absent"
    assert "bbox" not in payload, "bbox is omitted from to_dict() when absent"


def test_evidence_to_dict_includes_score_when_present_and_rounds_it():
    evidence = Evidence(
        evidence_id="E001",
        document_id="DOC-P101-HIST",
        document_version="1",
        page=4,
        text="...",
        classification="CONFIDENTIAL",
        acl=("maintenance",),
        provenance_id="E001",
        score=0.123456789,
    )
    assert evidence.to_dict()["score"] == 0.123457


def test_evidence_to_dict_includes_bbox_when_present():
    """packages/knowledge/AGENTS.md's citation contract: "document id,
    version, page, bounding box... a citation that cannot be pointed at is
    not a citation." bbox is new relative to the prototype's Evidence."""
    evidence = Evidence(
        evidence_id="E001",
        document_id="DOC-P101-HIST",
        document_version="1",
        page=4,
        text="...",
        classification="CONFIDENTIAL",
        acl=("maintenance",),
        provenance_id="E001",
        bbox=(0.12, 0.34, 0.56, 0.78),
    )
    payload = evidence.to_dict()
    assert payload["bbox"] == [0.12, 0.34, 0.56, 0.78]


def test_evidence_bbox_and_score_are_independent_optional_fields():
    evidence = Evidence(
        evidence_id="E001",
        document_id="DOC-P101-HIST",
        document_version="1",
        page=4,
        text="...",
        classification="CONFIDENTIAL",
        acl=("maintenance",),
        provenance_id="E001",
        score=0.9,
    )
    payload = evidence.to_dict()
    assert "score" in payload
    assert "bbox" not in payload


def test_evidence_provenance_id_is_never_anything_but_its_own_evidence_id_by_convention():
    """"Not a separate graph store -- provenance_id on an evidence row IS
    that row's own primary key." This module does not enforce that (it is
    the ingestion pipeline's job); this test documents the convention the
    shape exists to carry."""
    evidence = Evidence(
        evidence_id="E001",
        document_id="DOC-P101-HIST",
        document_version="1",
        page=4,
        text="...",
        classification="CONFIDENTIAL",
        acl=("maintenance",),
        provenance_id="E001",
    )
    assert evidence.provenance_id == evidence.evidence_id


def test_resource_build_matches_policy_resources_shape():
    resource = Resource.build("DOC-P101-HIST", "evidence", "CONFIDENTIAL", ["maintenance", "engineering"])
    assert resource.resource_id == "DOC-P101-HIST"
    assert resource.type == "evidence"
    assert resource.classification == "CONFIDENTIAL"
    assert resource.acl == ("maintenance", "engineering")


def test_resource_acl_defaults_to_empty():
    resource = Resource(resource_id="T123", type="task_scope", classification="PUBLIC")
    assert resource.acl == ()
