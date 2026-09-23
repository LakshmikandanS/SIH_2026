"""Knowledge: the door, ingestion of the real demo corpus, and ACL-filtered retrieval.

The integration tests ingest every file in ops/demo/corpus through the same upload and
worker path the product uses -- real Tesseract on the scans, real pgvector, a scripted
model runtime -- then search as each of the three seeded identities.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from citadel_contracts.domain import User
from citadel_gateway import AdmissionGate
from citadel_knowledge import (
    IngestContext,
    SearchScope,
    UploadRejected,
    claim_next,
    ingest,
    read_page,
    search,
    validate_metadata,
    visible_documents,
)
from citadel_knowledge.ocr import available as ocr_available
from citadel_knowledge.retrieval import REASON_ACL, REASON_CLASSIFICATION, evidence
from citadel_knowledge.seed import seed_corpus
from citadel_platform.db import Database
from citadel_platform.identity.store import get_user_by_external_identity
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector
from stack_fixtures import CORPUS, enabled_registry, gateway_for, start_fake

ENGINEER_1 = User("demo-engineer-1", "R. Kulkarni", ("engineer",), "internal", "process-engineering")
ENGINEER_2 = User("demo-engineer-2", "S. Nair", ("engineer",), "confidential", "instrumentation")


# -- the door (pure) --------------------------------------------------------------------


def _meta(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"classification": "internal", "acl": ["process-engineering"], "title": "t"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"classification": None}, "classification_missing"),
        ({"acl": None}, "acl_missing"),
        ({"acl": []}, "acl_missing"),
        ({"classification": "restricted"}, "unknown_classification"),
        ({"classification": "confidential"}, "exceeds_uploader_clearance"),
        ({"acl": ["instrumentation"]}, "uploader_not_in_acl"),
        ({"acl": ["Bad Dept!"]}, "acl_invalid"),
    ],
)
def test_the_door_rejects_loudly(overrides: dict[str, Any], code: str):
    with pytest.raises(UploadRejected) as caught:
        validate_metadata(_meta(**overrides), filename="x.pdf", profile_ceiling="CONFIDENTIAL", uploader=ENGINEER_1)
    assert caught.value.code == code


def test_the_profile_ceiling_is_enforced_at_ingest():
    """invariant 11: a PUBLIC-ceiling profile cannot hold INTERNAL material."""
    with pytest.raises(UploadRejected) as caught:
        validate_metadata(_meta(), filename="x.pdf", profile_ceiling="PUBLIC", uploader=ENGINEER_1)
    assert caught.value.code == "exceeds_profile_ceiling"


def test_a_valid_upload_is_normalised():
    meta = validate_metadata(
        _meta(classification="Internal", acl="process-engineering, quality-assurance"),
        filename="x.pdf",
        profile_ceiling="CONFIDENTIAL",
        uploader=ENGINEER_1,
    )
    assert meta.classification == "INTERNAL"
    assert meta.acl == ("process-engineering", "quality-assurance")


# -- ingestion and retrieval over the real corpus -----------------------------------------


@pytest.fixture(scope="module")
def corpus_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Database, Any]]:
    if not ocr_available():
        pytest.skip("tesseract is not installed on this machine")
    registry = enabled_registry()
    fake = start_fake(registry)
    try:
        with pg_scratch_db() as env:
            apply_all_migrations(env)
            db = Database(env=env)
            data_dir = DataDir(root=tmp_path_factory.mktemp("data"))
            summary = seed_corpus(
                db,
                data_dir,
                CORPUS / "manifest.yaml",
                profile_ceiling="CONFIDENTIAL",
                lookup_user=lambda external: get_user_by_external_identity(env, external),
            )
            assert summary["rejected"] == [], summary
            gateway = gateway_for(registry, fake)
            ctx = IngestContext(db=db, data_dir=data_dir, gateway=gateway, audit=None, tracer=Tracer(db), cpu=AdmissionGate("cpu", 2))
            while (document := claim_next(db, "test")) is not None:
                ingest(ctx, document)
            yield db, gateway
    finally:
        fake.stop()


@requires_pgvector
@pytest.mark.integration
def test_every_corpus_document_is_ready_with_an_honest_report(corpus_db: tuple[Database, Any]):
    db, _gateway = corpus_db
    rows = db.query("SELECT title, status, page_count, ingest_report FROM documents ORDER BY title")
    assert len(rows) == 7 and all(r["status"] == "ready" for r in rows)
    scanned = next(r for r in rows if "IR-2026-0147" in r["title"])
    report = scanned["ingest_report"]
    assert scanned["page_count"] == 2
    assert report["text_sources"] == {"ocr": 2}
    assert report["ocr_confidence"] and report["ocr_confidence"] > 70
    assert report["vision_pages"] == [1, 2]
    assert report["embedding_model"] and report["chunks"] >= 4
    born = next(r for r in rows if "CS-12" in r["title"])
    assert born["ingest_report"]["text_sources"] == {"text_layer": 1}
    assert born["ingest_report"]["vision_pages"] == []  # vision reads scans, not clean text layers


@requires_pgvector
@pytest.mark.integration
def test_ocr_blocks_carry_regions_and_numbers_survive(corpus_db: tuple[Database, Any]):
    db, _gateway = corpus_db
    rows = db.query(
        "SELECT b.text, b.bbox, b.source FROM document_blocks b JOIN documents d ON d.id = b.document_id "
        "WHERE d.title LIKE 'Inspection report%%' AND b.page = 2 ORDER BY b.block_index"
    )
    text = "\n".join(r["text"] for r in rows)
    assert "9.2 mm" in text and "0.25 mm/year" in text and "3.2 years" in text
    ocr_rows = [r for r in rows if r["source"] == "ocr"]
    assert ocr_rows and all(r["bbox"] and 0 <= r["bbox"][0] < r["bbox"][2] <= 1 for r in ocr_rows)
    assert any(r["source"] == "vision" and "Stamp" in r["text"] for r in rows)


@requires_pgvector
@pytest.mark.integration
def test_two_engineers_same_query_different_citations_and_denials(corpus_db: tuple[Database, Any]):
    """ADR-0001 §Q7, as a test: same query, visibly different citation sets, with the
    denial count and the reason for each denial."""
    db, gateway = corpus_db
    one = search(db, gateway, SearchScope("process-engineering", "INTERNAL", "demo-engineer-1"), "E-101 flange leak corrosion", top_k=10)
    two = search(db, gateway, SearchScope("instrumentation", "CONFIDENTIAL", "demo-engineer-2"), "E-101 flange leak corrosion", top_k=10)
    titles_one = {h.title for h in one.hits}
    titles_two = {h.title for h in two.hits}
    assert any("IR-2026-0147" in t for t in titles_one) and any("IR-2026-0147" in t for t in titles_two)
    assert not any("INC-2026-0092" in t for t in titles_one | titles_two)
    # every returned chunk is actually permitted -- filter before rank, checked after
    assert all(h.classification in ("public", "internal") and "process-engineering" in h.acl for h in one.hits)
    assert all("instrumentation" in h.acl for h in two.hits)

    incident = db.scalar("SELECT id::text FROM documents WHERE title LIKE 'Incident report%%'")
    denied_one = {d.document_id: d.reason for d in one.denied}
    denied_two = {d.document_id: d.reason for d in two.denied}
    assert denied_one[incident] == REASON_CLASSIFICATION
    assert denied_two[incident] == REASON_ACL
    # the denial record carries no text or title -- it cannot leak what it withholds
    assert set(one.denied[0].to_dict()) == {"document_id", "classification", "acl", "reason", "matching_chunks"}


@requires_pgvector
@pytest.mark.integration
def test_evidence_ids_are_task_scoped_and_resolve_to_a_page_region(corpus_db: tuple[Database, Any]):
    db, gateway = corpus_db
    task_id = "00000000-0000-4000-8000-000000000001"
    first = search(db, gateway, SearchScope("process-engineering", "INTERNAL", "demo-engineer-1"), "remaining life CML-3", task_id=task_id)
    again = search(db, gateway, SearchScope("process-engineering", "INTERNAL", "demo-engineer-1"), "remaining life CML-3", task_id=task_id)
    assert [h.evidence_id for h in first.hits] == [h.evidence_id for h in again.hits]
    assert first.hits[0].evidence_id == "E1"
    resolved = evidence(db, task_id, ["E1", "E99"])
    assert set(resolved) == {"E1"}
    assert resolved["E1"]["page"] >= 1 and resolved["E1"]["document_id"]


@requires_pgvector
@pytest.mark.integration
def test_read_page_enforces_the_same_predicate(corpus_db: tuple[Database, Any]):
    db, _gateway = corpus_db
    incident = str(db.scalar("SELECT id::text FROM documents WHERE title LIKE 'Incident report%%'"))
    assert read_page(db, SearchScope("process-engineering", "INTERNAL", "demo-engineer-1"), incident) is None
    visible = read_page(db, SearchScope("quality-assurance", "CONFIDENTIAL", "demo-approver"), incident)
    assert visible is not None and visible["blocks"]
    assert read_page(db, SearchScope("quality-assurance", "CONFIDENTIAL", "demo-approver"), "not-a-uuid") is None
    listing = visible_documents(db, SearchScope("instrumentation", "CONFIDENTIAL", "demo-engineer-2"))
    assert {r["title"].split(" ")[0] for r in listing} >= {"UT", "Calibration", "Nameplate"}
    assert not any("Incident" in r["title"] for r in listing)
    assert not any("pump P-310" in r["title"] for r in listing)
