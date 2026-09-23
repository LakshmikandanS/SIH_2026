"""vision.extract: re-read one region of one page with a vision model.

Normal extraction happens at ingest (ADR-0001 Gap 2), so by the time an agent reasons
about a scan it already has OCR text and the page-level vision reading. This tool is for
the rest: a stamp the OCR mangled, a handwritten field, a value in a table cell. The
region is cropped from the page image stored at ingest, sent through the gateway (which
routes to whichever vision-capable model is eligible and says why), and what comes back
becomes evidence pinned to that same document, version, page and region.
"""

from __future__ import annotations

import base64
import io
from typing import Any, Mapping

from PIL import Image

from citadel_knowledge import register_reading

from citadel_tools.context import Invocation, ResourceNotFound, ToolContext, ToolFailure, ToolOutput
from citadel_tools.docs import _document_resource
from citadel_tools.plugins import ToolPlugin

REGION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "fields"],
    "properties": {
        "text": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "value"],
                "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
            },
        },
        "legible": {"type": "boolean"},
    },
}

REGION_PROMPT = (
    "You are reading a cropped region of a scanned engineering document. Transcribe exactly "
    "what is visible -- printed text, handwriting, stamps, table values -- without guessing. "
    "Return JSON: text (a faithful transcription), fields (labelled values such as "
    "'Min thickness': '9.2 mm'), legible (false if the region cannot be read)."
)


def _crop(image: Image.Image, bbox: Any) -> Image.Image:
    if not bbox:
        return image
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ToolFailure("bbox is [x0, y0, x1, y1] as fractions of the page, each between 0 and 1, x0 < x1, y0 < y1")
    width, height = image.size
    margin = 0.01
    box = (
        int(max(0.0, x0 - margin) * width), int(max(0.0, y0 - margin) * height),
        int(min(1.0, x1 + margin) * width), int(min(1.0, y1 + margin) * height),
    )
    return image.crop(box)


def _run(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    resource = _document_resource(ctx, args)
    ctx.boundary.verify(invocation, resource)
    if ctx.gateway is None:
        raise ToolFailure("no model gateway is configured")
    document_id = resource.resource_id
    page_number = int(args["page"])
    row = ctx.db.query_one(
        "SELECT p.image_ref, d.version, d.title FROM document_pages p JOIN documents d "
        "ON d.id = p.document_id AND d.version = p.version WHERE p.document_id = %(d)s::uuid AND p.page = %(p)s",
        {"d": document_id, "p": page_number},
    )
    if row is None:
        raise ResourceNotFound(f"document {document_id} has no page {page_number}")
    if not row.get("image_ref"):
        raise ToolFailure("this page has no stored image to re-read (it was ingested from a text layer)")
    with Image.open(ctx.data_dir.resolve(str(row["image_ref"]))) as source:
        source.load()
        region = _crop(source.convert("RGB"), args.get("bbox"))
    region.thumbnail((1344, 1344))
    buffer = io.BytesIO()
    region.save(buffer, "JPEG", quality=88)
    ctx.emit(kind="model", state="waiting", note="vision model reading the region")
    result = ctx.gateway.vision(
        REGION_PROMPT,
        [base64.b64encode(buffer.getvalue()).decode("ascii")],
        classification=ctx.task_classification,
        schema=REGION_SCHEMA,
        purpose="vision.extract",
        task_id=ctx.task_id,
        actor_id=ctx.user.user_id,
    )
    reading: Mapping[str, Any] = result.data if isinstance(result.data, Mapping) else {"text": result.text, "fields": []}
    fields = [f for f in reading.get("fields") or [] if isinstance(f, Mapping)]
    text = str(reading.get("text") or "").strip()
    if fields:
        text = (text + "\n" if text else "") + "; ".join(f"{f.get('label')}: {f.get('value')}" for f in fields)
    bbox = [float(v) for v in args["bbox"]] if args.get("bbox") else None
    evidence_id = register_reading(
        ctx.db,
        ctx.task_id,
        document_id=document_id,
        version=int(row["version"]),
        page=page_number,
        bbox=bbox,
        text=f"Region re-read by vision model: {text}" if text else "Region re-read by vision model: (nothing legible)",
        classification=resource.classification,
        detail={"title": row["title"], "model_id": result.model_id, "tool": "vision.extract", "legible": reading.get("legible", True)},
    )
    return ToolOutput(
        data={
            "evidence_id": evidence_id,
            "document": row["title"],
            "page": page_number,
            "bbox": bbox,
            "text": text[:3000],
            "fields": fields[:30],
            "model": result.model_id,
            "how_to_cite": f"Cite this reading as [{evidence_id}].",
        },
        summary=f"re-read page {page_number} of {row['title']} with {result.model_id} [{evidence_id}]",
        evidence=[evidence_id],
        detail={"routing": result.meta() if hasattr(result, "meta") else {}},
    )


PLUGINS = {"vision.extract": ToolPlugin("vision.extract", _document_resource, _run)}

__all__ = ["PLUGINS", "REGION_SCHEMA", "REGION_PROMPT"]
