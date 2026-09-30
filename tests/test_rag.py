"""Design doc section 9 -- RAG. Owned by `data-plane-rag` (step 6).

Against the real demo corpus (`data/`, documented in `data/README.md`) and
the real, locally-serving Ollama embedding model -- no mocked embeddings.
`populated_store` ingests the two legitimate documents once per module
(ingestion makes a real network call per chunk, so this is not repeated per
test); `test_document_missing_its_acl_sidecar_is_rejected_at_ingestion` and
`test_ingest_directory_rejects_the_whole_corpus` use their own throwaway
stores because they are specifically testing that nothing gets ingested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capability import issue_for_step
from app.db.models import Agent
from app.db.state_machines import Classification
from app.policy.tools import Tool
from app.rag.backend import rag_search_backend
from app.rag.ingest import (
    MissingSidecarError,
    ingest_directory,
    ingest_document,
    ingest_paths,
)
from app.rag.search import Requester, search
from app.rag.store import VectorStore, set_store
from app.tool_gateway import clear_backends, invoke, register_backend, task_resource

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
HISTORY_DOC = DATA_ROOT / "maintenance" / "pump_p101_history.txt"
SPEC_DOC = DATA_ROOT / "maintenance" / "pump_p101_spec_sheet.txt"
FINANCE_DOC = DATA_ROOT / "finance" / "q3_finance_report.txt"
UNTAGGED_DOC = DATA_ROOT / "maintenance" / "pump_p102_notes.txt"

MAINTENANCE_QUERY = "Pump P-101 maintenance history"
FINANCE_QUERY = "Q3 finance report"  # design doc section 1.2's own denial-path query

# The requester block the Tool Gateway builds from a verified capability's
# scope (design doc section 6.9) -- never agent-supplied.
CONFIDENTIAL_MAINTENANCE = Requester(
    task_id="T123",
    agent_id="A123",
    classification_max=Classification.CONFIDENTIAL,
    department="maintenance",
)
INTERNAL_MAINTENANCE = Requester(
    task_id="T-INTERNAL",
    agent_id="A-INTERNAL",
    classification_max=Classification.INTERNAL,
    department="maintenance",
)


@pytest.fixture(scope="module")
def populated_store() -> VectorStore:
    """The two legitimate maintenance-department documents plus the one
    finance document, ingested once. `pump_p102_notes.txt` (no sidecar) is
    deliberately not included here -- ingesting it is exactly what the
    dedicated rejection tests below exercise."""
    store = VectorStore()
    ingest_paths([HISTORY_DOC, SPEC_DOC, FINANCE_DOC], store=store)
    return store


# ---------------------------------------------------------------------------
# The four checklist lines, plus the two extra cases requested for step 6.
# ---------------------------------------------------------------------------


def test_authorized_document_is_retrieved(populated_store):
    """Correctly-tagged, authorized document -> retrieved."""
    outcome = search(MAINTENANCE_QUERY, CONFIDENTIAL_MAINTENANCE, store=populated_store)

    assert outcome.results, "expected at least one result for an authorized query"
    document_ids = {row["document_id"] for row in outcome.results}
    assert "DOC-P101-HIST" in document_ids

    # The evidence rows are the section 6.9 contract shape, not a superset
    # that happens to also work.
    for row in outcome.results:
        assert row["classification"] in (Classification.PUBLIC, Classification.INTERNAL, Classification.CONFIDENTIAL)
        assert set(row["acl"]) & {CONFIDENTIAL_MAINTENANCE.department}


def test_out_of_scope_document_is_filtered_before_reaching_the_agent(populated_store):
    """Correctly-tagged, out-of-scope document -> filtered before reaching
    the agent -- not returned and then discarded by the caller.

    This is design doc section 1.2's exact denial-path query
    ("Q3 finance report"), issued by a `department: maintenance` requester
    against `acl: ["finance"]` documents: zero results come back from this
    layer, before the Tool Gateway's own Policy Engine check would even run.
    """
    outcome = search(FINANCE_QUERY, CONFIDENTIAL_MAINTENANCE, store=populated_store)

    assert outcome.results == []
    assert all(row["document_id"] != "DOC-FIN-Q3" for row in outcome.results)
    assert any(d.document_id == "DOC-FIN-Q3" for d in outcome.filtered_documents)
    assert outcome.filtered_document_count >= 1
    assert outcome.filtered_chunk_count >= 1

    # The denial record itself never carries the document's text -- an
    # unauthorized document is never even constructed into a result, and the
    # audit trail about it does not smuggle the content back out either.
    for denied in outcome.filtered_documents:
        assert not hasattr(denied, "text")
        assert "text" not in denied.to_dict()


def test_document_missing_its_acl_sidecar_is_rejected_at_ingestion():
    """Document missing its ACL sidecar -> ingestion rejected outright, with
    a clear error -- not a silent skip, not a default classification."""
    store = VectorStore()

    with pytest.raises(MissingSidecarError) as excinfo:
        ingest_document(UNTAGGED_DOC, store=store)

    assert "pump_p102_notes.txt" in str(excinfo.value)
    assert len(store) == 0, "a rejected document must add nothing to the store"


def test_ingest_directory_rejects_the_whole_corpus_when_one_sidecar_is_missing():
    """The directory-level entry point fails outright too: `data/maintenance`
    contains the untagged fixture alongside two legitimate documents, and
    ingesting the directory must not silently ingest only the good ones."""
    store = VectorStore()

    with pytest.raises(MissingSidecarError):
        ingest_directory(DATA_ROOT / "maintenance", store=store)

    assert len(store) == 0


def test_classification_above_requesters_max_is_filtered(populated_store):
    """An INTERNAL task cannot see a CONFIDENTIAL document, even though the
    ACL overlaps and the query matches it well. The lattice is
    PUBLIC < INTERNAL < CONFIDENTIAL (app/db/state_machines.py); a
    CONFIDENTIAL task can never be out-ranked, so this needs an INTERNAL
    requester reaching for the CONFIDENTIAL history document (data/README.md)."""
    outcome = search(MAINTENANCE_QUERY, INTERNAL_MAINTENANCE, store=populated_store)

    assert all(row["document_id"] != "DOC-P101-HIST" for row in outcome.results)
    assert any(
        d.document_id == "DOC-P101-HIST" and d.reason == "classification_exceeds_max"
        for d in outcome.filtered_documents
    )
    # The INTERNAL spec sheet for the same asset is still visible -- this is
    # a classification filter, not a department/ACL one.
    assert any(row["document_id"] == "DOC-P101-SPEC" for row in outcome.results)


def test_evidence_rows_carry_provenance_id(populated_store):
    """provenance_id IS the evidence row's own primary key (section 6.9) --
    there is no separate provenance graph in this slice."""
    outcome = search(MAINTENANCE_QUERY, CONFIDENTIAL_MAINTENANCE, store=populated_store)

    assert outcome.results
    for row in outcome.results:
        assert row["provenance_id"] == row["evidence_id"]
        assert row["evidence_id"]  # non-empty


# ---------------------------------------------------------------------------
# End to end: through invoke(), not the module directly. This is the proof
# the wiring (capability -> policy -> Data Plane) actually holds together,
# not just that app.rag.search filters correctly in isolation.
# ---------------------------------------------------------------------------


# `db`, `engineer` and `task` come from tests/conftest.py -- a fresh schema
# per test, one CONFIDENTIAL/maintenance engineer (U123) and task (T123),
# already matching `CONFIDENTIAL_MAINTENANCE` above. Only the Agent row is
# added here (conftest has no `agent` fixture; test_security.py's is not
# importable from this file, per this step's file-ownership boundary).
@pytest.fixture
def agent(db, task):
    row = Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher")
    db.add(row)
    db.commit()
    return row


def test_rag_search_through_the_tool_gateway_end_to_end(
    db, task, agent, populated_store
):
    """The whole path: capability issued -> `invoke()` -> Step A -> Step B
    (ALLOW) -> Step C dispatches to the real `rag.search` backend -> the
    Data Plane's own ACL/classification filtering runs -> the envelope comes
    back with only authorized evidence. Not a direct call into
    `app.rag.search` -- this is what proves the backend is actually wired the
    way `app/tool_gateway/backends/echo.py`'s docstring describes.
    """
    previous_store = set_store(populated_store)
    register_backend(Tool.RAG_SEARCH, rag_search_backend)
    try:
        capability = issue_for_step(
            task.task_id, "A123", Tool.RAG_SEARCH
        )
        envelope = invoke(
            capability_token=capability.token,
            tool=Tool.RAG_SEARCH,
            resource=task_resource(task.task_id),
            arguments={"query": MAINTENANCE_QUERY},
        )

        assert envelope["success"] is True
        assert envelope["error"] is None
        results = envelope["result"]["results"]
        assert results
        assert any(row["document_id"] == "DOC-P101-HIST" for row in results)
        assert all(row["document_id"] != "DOC-FIN-Q3" for row in results)

        # EVIDENCE_RETRIEVED was emitted by the backend, distinct from the
        # gateway's own four events, and it is what the denial-path demo
        # reads to show "N filtered for ACL/classification reasons".
        from app.observability import get_trace

        evidence_events = [
            e for e in get_trace(task.task_id) if e.event_type == "EVIDENCE_RETRIEVED"
        ]
        assert len(evidence_events) == 1
        assert evidence_events[0].payload["returned_count"] == len(results)
    finally:
        set_store(previous_store)
        clear_backends()


def test_rag_search_through_the_tool_gateway_denies_finance_department_leak(
    db, task, agent, populated_store
):
    """Same wiring, but the denial-path query (section 1.2): a
    `department: maintenance` task asking for `acl: ["finance"]` content
    gets zero results back from the envelope -- the Data Plane's own
    filtering, not a Policy Engine denial (Policy ALLOWs `rag.search` as an
    operation here; `task_resource` carries the task's own envelope, and this
    call never names the finance document as its `resource`, exactly as
    section 6.9 describes: the concrete documents are not known until the
    Data Plane itself looks them up)."""
    previous_store = set_store(populated_store)
    register_backend(Tool.RAG_SEARCH, rag_search_backend)
    try:
        capability = issue_for_step(
            task.task_id, "A123", Tool.RAG_SEARCH
        )
        envelope = invoke(
            capability_token=capability.token,
            tool=Tool.RAG_SEARCH,
            resource=task_resource(task.task_id),
            arguments={"query": FINANCE_QUERY},
        )

        assert envelope["success"] is True
        assert envelope["result"]["results"] == []
    finally:
        set_store(previous_store)
        clear_backends()
