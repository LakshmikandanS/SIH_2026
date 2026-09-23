"""Ingestion (OCR and vision at ingest), ACL-filtered hybrid retrieval with the denial
record kept, and citations pinned to document, version, page and region."""

from __future__ import annotations

from citadel_knowledge.ingest import IngestContext, IngestReport, claim_next, fail, ingest
from citadel_knowledge.retrieval import (
    LATTICE,
    DeniedDocument,
    SearchHit,
    SearchResult,
    SearchScope,
    document_facts,
    evidence,
    is_uuid,
    read_page,
    register_computation,
    register_evidence,
    register_reading,
    search,
    task_evidence,
    visible_documents,
)
from citadel_knowledge.upload import UploadMetadata, UploadRejected, register_upload, validate_metadata

__all__ = [
    "IngestContext",
    "IngestReport",
    "claim_next",
    "fail",
    "ingest",
    "LATTICE",
    "DeniedDocument",
    "SearchHit",
    "SearchResult",
    "SearchScope",
    "document_facts",
    "evidence",
    "is_uuid",
    "read_page",
    "register_evidence",
    "register_reading",
    "register_computation",
    "task_evidence",
    "search",
    "visible_documents",
    "UploadMetadata",
    "UploadRejected",
    "register_upload",
    "validate_metadata",
]
