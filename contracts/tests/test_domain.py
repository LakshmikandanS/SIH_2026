"""contracts/domain.py -- the plain schemas hold exactly the fields design
doc §3 names, and each is a frozen (immutable) value object."""

from __future__ import annotations

import dataclasses

import pytest

from contracts.domain import Agent, Approval, Artifact, Evidence, Resource, Task, User


def test_user_carries_no_password_hash():
    """The one deliberate omission from the module docstring: a User
    crossing this boundary never carries the secret, even though the ORM
    row it mirrors has a column for it."""
    fields = {f.name for f in dataclasses.fields(User)}
    assert "password_hash" not in fields
    assert fields == {"user_id", "username", "roles", "clearance", "department", "created_at"}


def test_task_carries_no_department_field():
    """Department is an attribute of the owning User, not the Task -- see
    the module docstring and `app/policy/context.py::PolicyTask`."""
    fields = {f.name for f in dataclasses.fields(Task)}
    assert "department" not in fields


def test_domain_objects_are_frozen():
    user = User(user_id="U123", username="j.rao")
    with pytest.raises(dataclasses.FrozenInstanceError):
        user.username = "someone.else"  # type: ignore[misc]


def test_agent_round_trips_the_design_doc_shape():
    agent = Agent(agent_id="A123", task_id="T123", agent_type="researcher", status="RUNNING")
    assert agent.agent_id == "A123"
    assert agent.task_id == "T123"


def test_artifact_defaults_match_the_initial_state_machine_state():
    from contracts.state_machines import ArtifactStatus

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


def test_evidence_to_dict_is_the_design_doc_wire_shape():
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


def test_evidence_provenance_id_is_never_anything_but_its_own_evidence_id_by_convention():
    """Design doc §6.9: "not a separate graph store -- provenance_id on an
    evidence row IS that row's own primary key." This module does not
    enforce that (it is `Evidence.from_chunk`'s job, which stayed in
    `app/rag/evidence.py`); this test documents the convention the shape
    exists to carry."""
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
