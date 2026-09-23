"""The ingestion pipeline: detect -> extract | OCR | vision -> normalise -> chunk ->
embed -> index (packages/knowledge/AGENTS.md).

Runs in a worker, never in an HTTP request, behind the CPU admission gate (ADR-0004:
on one box OCR shares the machine with Ollama, so bulk ingestion must not be allowed
to starve everything else). Every degradation -- no OCR engine, no vision model, no
embedding model -- is recorded in the document's ingest report and in the audit event,
and the document is still made as useful as what did work allows: a document indexed
without embeddings is searchable lexically, and says so.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

from citadel_gateway import AdmissionGate, Gateway, NoEligibleModel, ProviderError
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database, Json, RealArray, Vector
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer

from citadel_knowledge import extract, ocr
from citadel_knowledge.chunking import Chunk, chunk_page
from citadel_knowledge.normalise import PageContent
from citadel_knowledge.vision_ingest import read_page, vision_blocks

#: Vision is a model swap on the 8 GB profile; it reads the pages that need it (scans
#: and images), and at most this many per document.
MAX_VISION_PAGES = 4

Progress = Callable[[dict[str, Any]], None]


@dataclass
class IngestContext:
    db: Database
    data_dir: DataDir
    gateway: Optional[Gateway]
    audit: Optional[AuditLog]
    tracer: Tracer
    cpu: AdmissionGate
    worker_id: str = "worker"


@dataclass
class IngestReport:
    pages: int = 0
    text_sources: dict[str, int] = field(default_factory=dict)
    ocr_confidence: Optional[float] = None
    low_confidence_blocks: int = 0
    vision_pages: list[int] = field(default_factory=list)
    vision_model: Optional[str] = None
    embedding_model: Optional[str] = None
    chunks: int = 0
    blocks: int = 0
    degraded: list[str] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    cpu_wait_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "text_sources": self.text_sources,
            "ocr_confidence": self.ocr_confidence,
            "low_confidence_blocks": self.low_confidence_blocks,
            "vision_pages": self.vision_pages,
            "vision_model": self.vision_model,
            "embedding_model": self.embedding_model,
            "chunks": self.chunks,
            "blocks": self.blocks,
            "degraded": self.degraded,
            "timings_ms": self.timings_ms,
            "cpu_wait_ms": self.cpu_wait_ms,
        }


def claim_next(db: Database, worker_id: str) -> Optional[dict[str, Any]]:
    """Take the oldest pending document; SKIP LOCKED lets many workers pull at once."""
    return db.query_one(
        "UPDATE documents SET status = 'processing', ingest_report = %(report)s "
        "WHERE id = (SELECT id FROM documents WHERE status = 'pending' ORDER BY created_at "
        "FOR UPDATE SKIP LOCKED LIMIT 1) "
        "RETURNING id::text AS id, title, filename, mime_type, classification, acl, version, "
        "storage_ref, uploaded_by",
        {"report": Json({"stage": "claimed", "worker": worker_id})},
    )


def embedding_dimensions(db: Database) -> Optional[int]:
    """The vector column's declared width -- the schema, not this code, decides it."""
    value = db.scalar(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'document_chunks'::regclass "
        "AND attname = 'embedding'"
    )
    return int(value) if isinstance(value, int) and value > 0 else None


def _set_stage(db: Database, document_id: str, stage: dict[str, Any]) -> None:
    db.execute(
        "UPDATE documents SET ingest_report = %(r)s WHERE id = %(id)s::uuid",
        {"r": Json(stage), "id": document_id},
    )


def _load_pages(path: Path, kind: str) -> tuple[list[PageContent], list[Optional[Image.Image]]]:
    if kind == "pdf":
        pages = extract.pdf_pages(path)
        images: list[Optional[Image.Image]] = list(extract.render_pdf(path))
        return pages, images
    if kind == "image":
        return [PageContent(page=1, text_source="none")], [extract.load_image(path)]
    if kind == "docx":
        pages = extract.docx_pages(path)
        return pages, [None] * len(pages)
    pages = extract.text_pages(path)
    return pages, [None] * len(pages)


def ingest(ctx: IngestContext, document: dict[str, Any], *, progress: Optional[Progress] = None) -> IngestReport:
    document_id = str(document["id"])
    version = int(document["version"])
    classification = str(document["classification"]).upper()
    actor = str(document["uploaded_by"])
    report = IngestReport()
    path = ctx.data_dir.resolve(str(document["storage_ref"]))

    def stage(name: str, **detail: Any) -> None:
        payload = {"stage": name, **detail}
        _set_stage(ctx.db, document_id, payload)
        if progress is not None:
            progress(payload)

    with ctx.tracer.span("ingest.document", "ingest", attributes={"document_id": document_id}) as span:
        # ---- CPU-bound extraction, behind the CPU admission gate -------------------
        stage("waiting_for_cpu")
        with ctx.cpu.acquire() as waited:
            report.cpu_wait_ms = waited
            started = time.perf_counter()
            stage("extracting")
            kind, _mime = extract.detect_kind(str(document["filename"]), path.read_bytes()[:16])
            pages, images = _load_pages(path, kind)
            report.timings_ms["extract"] = int((time.perf_counter() - started) * 1000)

            page_dir = ctx.data_dir.document_dir(document_id, version)
            confidences: list[float] = []
            for page, image in zip(pages, images):
                if image is not None:
                    image_path = page_dir / f"page-{page.page}.jpg"
                    image_path.write_bytes(extract.to_jpeg(image, max_side=1800))
                    page.image_ref = ctx.data_dir.relative(image_path)
                    page.width_px, page.height_px = image.size
                if page.text_source == "none" and image is not None:
                    stage("ocr", page=page.page, pages=len(pages))
                    started = time.perf_counter()
                    try:
                        page.blocks, page.ocr_confidence = ocr.ocr_blocks(image)
                        page.text_source = "ocr" if page.blocks else "none"
                        if page.ocr_confidence is not None:
                            confidences.append(page.ocr_confidence)
                    except ocr.OCRUnavailable as exc:
                        report.degraded.append(f"page {page.page}: OCR unavailable ({exc}); text not extracted")
                    report.timings_ms["ocr"] = report.timings_ms.get("ocr", 0) + int((time.perf_counter() - started) * 1000)
            report.ocr_confidence = round(sum(confidences) / len(confidences), 1) if confidences else None

        # ---- vision at ingest (a GPU call: the gateway's own admission applies) ----
        scan_pages = [(p, img) for p, img in zip(pages, images) if img is not None and p.text_source != "text_layer"]
        if scan_pages and ctx.gateway is not None:
            for page, image in scan_pages[:MAX_VISION_PAGES]:
                assert image is not None
                stage("vision", page=page.page, pages=len(pages))
                started = time.perf_counter()
                try:
                    reading = read_page(ctx.gateway, image, classification=classification, actor_id=actor)
                    page.vision = reading
                    page.blocks.extend(vision_blocks(reading))
                    report.vision_pages.append(page.page)
                    report.vision_model = str(reading.get("model_id"))
                except (NoEligibleModel, ProviderError) as exc:
                    reason = str(exc)[:300]
                    page.vision = {"status": "skipped", "reason": reason}
                    report.degraded.append(f"page {page.page}: vision reading skipped ({reason[:160]})")
                report.timings_ms["vision"] = report.timings_ms.get("vision", 0) + int((time.perf_counter() - started) * 1000)
            if len(scan_pages) > MAX_VISION_PAGES:
                report.degraded.append(
                    f"vision read the first {MAX_VISION_PAGES} of {len(scan_pages)} scanned pages; the rest have OCR text only"
                )
        elif scan_pages:
            report.degraded.append("no model gateway in this process: scanned pages have OCR text only")

        # ---- chunk and embed --------------------------------------------------------
        stage("indexing")
        chunks: list[Chunk] = []
        for page in pages:
            chunks.extend(chunk_page(page, start_index=len(chunks)))
        embeddings: list[Optional[list[float]]] = [None] * len(chunks)
        dims = embedding_dimensions(ctx.db)
        if chunks and ctx.gateway is not None:
            started = time.perf_counter()
            try:
                result = ctx.gateway.embed(
                    [f"{document['title']}\n{c.text}" for c in chunks], classification=classification, actor_id=actor
                )
                if dims is not None and result.dimensions != dims:
                    report.degraded.append(
                        f"embedding model {result.model_id} returns {result.dimensions} dimensions but the index "
                        f"stores {dims}; indexed for lexical search only"
                    )
                else:
                    embeddings = [list(v) for v in result.vectors]
                    report.embedding_model = result.model_id
            except (NoEligibleModel, ProviderError) as exc:
                report.degraded.append(f"no embedding model available ({str(exc)[:160]}); indexed for lexical search only")
            report.timings_ms["embed"] = int((time.perf_counter() - started) * 1000)
        elif chunks:
            report.degraded.append("no model gateway in this process; indexed for lexical search only")

        # ---- persist everything in one transaction ------------------------------------
        statements: list[tuple[str, Optional[dict[str, Any]]]] = []
        block_ids: dict[tuple[int, int], str] = {}
        for page in pages:
            statements.append((
                "INSERT INTO document_pages (document_id, version, page, width_px, height_px, image_ref, "
                "text_source, ocr_confidence, vision) VALUES (%(doc)s::uuid, %(v)s, %(page)s, %(w)s, %(h)s, "
                "%(img)s, %(src)s, %(conf)s, %(vision)s)",
                {
                    "doc": document_id, "v": version, "page": page.page, "w": page.width_px,
                    "h": page.height_px, "img": page.image_ref, "src": page.text_source,
                    "conf": page.ocr_confidence, "vision": Json(page.vision) if page.vision is not None else None,
                },
            ))
            for index, block in enumerate(page.blocks):
                block_id = str(uuid.uuid4())
                block_ids[(page.page, index)] = block_id
                if block.low_confidence:
                    report.low_confidence_blocks += 1
                statements.append((
                    "INSERT INTO document_blocks (id, document_id, version, page, block_index, kind, source, "
                    "text, bbox, confidence, low_confidence, content_hash) VALUES (%(id)s::uuid, %(doc)s::uuid, "
                    "%(v)s, %(page)s, %(i)s, %(kind)s, %(source)s, %(text)s, %(bbox)s, %(conf)s, %(low)s, %(hash)s)",
                    {
                        "id": block_id, "doc": document_id, "v": version, "page": page.page, "i": index,
                        "kind": block.kind, "source": block.source, "text": block.text,
                        "bbox": RealArray(block.bbox) if block.bbox else None, "conf": block.confidence,
                        "low": block.low_confidence, "hash": block.content_hash,
                    },
                ))
            report.blocks += len(page.blocks)
        for chunk, vector in zip(chunks, embeddings):
            statements.append((
                "INSERT INTO document_chunks (classification, acl, content, embedding, document_id, version, "
                "page, chunk_index, bbox, block_ids, embedding_model, tsv) VALUES (%(c)s, %(acl)s, %(content)s, "
                "%(emb)s, %(doc)s::uuid, %(v)s, %(page)s, %(i)s, %(bbox)s, %(blocks)s::uuid[], %(model)s, "
                "setweight(to_tsvector('english', %(title)s), 'A') || setweight(to_tsvector('english', %(content)s), 'B'))",
                {
                    "c": str(document["classification"]).lower(), "acl": list(document["acl"]),
                    "content": chunk.text, "emb": Vector(vector) if vector is not None else None,
                    "doc": document_id, "v": version, "page": chunk.page, "i": chunk.index,
                    "bbox": RealArray(chunk.bbox) if chunk.bbox else None,
                    "blocks": [block_ids[(chunk.page, i)] for i in chunk.block_indexes],
                    "model": report.embedding_model if vector is not None else None,
                    "title": str(document["title"]),
                },
            ))
        report.pages = len(pages)
        report.chunks = len(chunks)
        for page in pages:
            report.text_sources[page.text_source] = report.text_sources.get(page.text_source, 0) + 1
        if not chunks:
            report.degraded.append("no text could be extracted from any page")
        statements.append((
            "UPDATE documents SET status = 'ready', page_count = %(pages)s, ingest_report = %(report)s, "
            "error = NULL WHERE id = %(id)s::uuid",
            {"pages": len(pages), "report": Json({"stage": "ready", **report.to_dict()}), "id": document_id},
        ))
        ctx.db.script(statements)
        span.set("report", report.to_dict())

    if ctx.audit is not None:
        ctx.audit.record(
            "document.ingested",
            actor_id=actor,
            payload={
                "document_id": document_id,
                "title": str(document["title"]),
                "classification": classification,
                "acl": list(document["acl"]),
                "pages": report.pages,
                "chunks": report.chunks,
                "vision_model": report.vision_model,
                "embedding_model": report.embedding_model,
                "degraded": report.degraded,
            },
        )
    return report


def fail(ctx: IngestContext, document: dict[str, Any], error: str) -> None:
    ctx.db.execute(
        "UPDATE documents SET status = 'failed', error = %(e)s WHERE id = %(id)s::uuid",
        {"e": error[:2000], "id": str(document["id"])},
    )
    if ctx.audit is not None:
        ctx.audit.record(
            "document.rejected",
            actor_id=str(document["uploaded_by"]),
            payload={"document_id": str(document["id"]), "stage": "ingest", "error": error[:500]},
        )


__all__ = ["IngestContext", "IngestReport", "claim_next", "ingest", "fail", "embedding_dimensions", "MAX_VISION_PAGES"]
