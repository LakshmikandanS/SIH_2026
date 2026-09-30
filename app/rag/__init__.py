"""Data Plane / RAG (design doc section 6.9).

One vector store, not two systems: "Organizational Knowledge" and the RAG
store are declared identical for this MVP (section 6.9). Ingestion requires a
mandatory ACL sidecar per document, with no default and no inference
(`app.rag.sidecar`); retrieval filters by ACL and classification strictly
inside this package, before a result is ever constructed
(`app.rag.search`); the Tool Gateway backend
(`app.rag.backend.rag_search_backend`) is what step 7 (or a test, or the
demo) attaches with:

    from app.policy import Tool
    from app.tool_gateway import register_backend
    from app.rag.backend import rag_search_backend

    register_backend(Tool.RAG_SEARCH, rag_search_backend)

Working Memory (section 6.9) lives only in the Orchestrator's in-process call
stack for one task execution -- there is deliberately no storage for it here
or anywhere in this package.
"""

from app.rag.backend import rag_search_backend
from app.rag.evidence import Evidence
from app.rag.ingest import (
    IngestedDocument,
    IngestionReport,
    InvalidSidecarError,
    MissingSidecarError,
    SidecarError,
    discover_documents,
    ingest_directory,
    ingest_document,
    ingest_paths,
)
from app.rag.search import DeniedDocument, Requester, SearchOutcome, search
from app.rag.store import Chunk, VectorStore, get_store, reset_store, set_store

__all__ = [
    "Chunk",
    "DeniedDocument",
    "Evidence",
    "IngestedDocument",
    "IngestionReport",
    "InvalidSidecarError",
    "MissingSidecarError",
    "Requester",
    "SearchOutcome",
    "SidecarError",
    "VectorStore",
    "discover_documents",
    "get_store",
    "ingest_directory",
    "ingest_document",
    "ingest_paths",
    "rag_search_backend",
    "reset_store",
    "search",
    "set_store",
]
