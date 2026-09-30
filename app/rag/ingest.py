"""Ingestion (design doc section 6.9, BB-036).

Walks a directory, chunks and embeds each document, and adds the result to a
`VectorStore` -- but only for a document that has a valid sidecar. A document
without one is not skipped quietly: `ingest_document` raises immediately
(`app.rag.sidecar.MissingSidecarError`), and `ingest_paths`/`ingest_directory`
let that exception propagate rather than catching and continuing. That is the
whole of section 6.9's "ingestion rejects outright any document missing one
... no silent skip" for this module: the very first bad document stops the
batch, loudly, with the offending path named in the error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

from app.rag import settings
from app.rag.chunking import chunk_text
from app.rag.embeddings import embed_text
from app.rag.sidecar import (
    InvalidSidecarError,
    MissingSidecarError,
    SidecarError,
    load_sidecar,
)
from app.rag.store import Chunk, VectorStore

__all__ = [
    "IngestedDocument",
    "IngestionReport",
    "InvalidSidecarError",
    "MissingSidecarError",
    "SidecarError",
    "discover_documents",
    "ingest_directory",
    "ingest_document",
    "ingest_paths",
]


@dataclass(frozen=True)
class IngestedDocument:
    document_id: str
    document_version: str
    path: str
    chunk_count: int


@dataclass(frozen=True)
class IngestionReport:
    documents: list[IngestedDocument]

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def chunk_count(self) -> int:
        return sum(d.chunk_count for d in self.documents)


def discover_documents(root: Union[str, Path]) -> list[Path]:
    """Every file under `root` that ingestion considers a source document:
    not a sidecar itself, not an excluded filename (`README.md`).

    This does NOT check that a sidecar exists -- it only decides what counts
    as "a document" in the first place. `ingest_document` is what enforces
    section 6.9's mandatory-sidecar rule, one file at a time.
    """
    root = Path(root)
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in settings.EXCLUDED_FILENAMES:
            continue
        if path.name.endswith(settings.SIDECAR_SUFFIX):
            continue
        found.append(path)
    return found


def ingest_document(path: Union[str, Path], *, store: VectorStore) -> IngestedDocument:
    """Ingest exactly one document into `store`.

    Raises `MissingSidecarError` / `InvalidSidecarError` (section 6.9) before
    touching the store at all if the sidecar is missing or unusable -- nothing
    is added to `store` on failure, chunked or otherwise.
    """
    path = Path(path)
    meta = load_sidecar(path)  # raises on a missing/invalid sidecar; no default

    text = path.read_text(encoding="utf-8")
    pieces = chunk_text(text, min_chunk_chars=settings.MIN_CHUNK_CHARS)
    if not pieces:
        # Not a sidecar problem -- the metadata was fine, the document body
        # itself is empty. Distinct failure, distinct exception type.
        raise ValueError(f"{path} has no ingestible text (empty after chunking)")

    document_id = meta["document_id"]
    document_version = str(meta["document_version"])
    classification = meta["classification"]
    acl = tuple(meta["acl"])
    department = meta["department"]

    chunks = [
        Chunk(
            document_id=document_id,
            document_version=document_version,
            document_path=str(path),
            department=department,
            classification=classification,
            acl=acl,
            page=page,
            text=body,
            embedding=embed_text(body),
        )
        for page, body in enumerate(pieces, start=1)
    ]
    for chunk in chunks:
        store.add(chunk)

    return IngestedDocument(
        document_id=document_id,
        document_version=document_version,
        path=str(path),
        chunk_count=len(chunks),
    )


def ingest_paths(paths: Iterable[Union[str, Path]], *, store: VectorStore) -> IngestionReport:
    """Ingest an explicit list of documents, atomically with respect to the
    sidecar check: every document's sidecar is validated *first*, in order,
    before anything is chunked, embedded, or added to `store`. The first
    missing or invalid sidecar raises immediately and `store` is left exactly
    as it was -- a batch that is rejected never leaves a partial trace of
    the documents that would have succeeded, which would otherwise look like
    a silent partial skip of whichever document failed.
    """
    paths = [Path(p) for p in paths]
    for path in paths:
        load_sidecar(path)  # raises; nothing has been added to `store` yet

    documents = [ingest_document(path, store=store) for path in paths]
    return IngestionReport(documents=documents)


def ingest_directory(root: Union[str, Path], *, store: VectorStore) -> IngestionReport:
    """Discover and ingest every document under `root`.

    A corpus containing even one document without a sidecar (the demo corpus
    does, deliberately -- see data/README.md) makes this call raise. That is
    section 6.9's "ingestion rejects outright", applied at the batch level:
    fix the corpus, then ingest it, rather than silently building a store
    that is missing a document nobody was told about.
    """
    return ingest_paths(discover_documents(root), store=store)
