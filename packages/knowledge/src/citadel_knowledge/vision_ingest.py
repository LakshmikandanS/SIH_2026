"""Vision at ingest, not in the agent loop (ADR-0001 Gap 2, kept by ADR-0002).

When a document arrives, each page image goes once to whichever vision-capable model
the gateway routes to. What comes back -- stamps, signatures, handwritten form fields,
labelled values, a one-line reading of the page -- is persisted beside the OCR text, so
by the time an agent reasons about the document it reads rows, not pixels. On the 8 GB
profile this keeps the vision model's swap on an upload progress bar instead of in the
middle of a task.

Vision findings carry no bounding box: the models that fit on the demonstration card do
not localise reliably, so their blocks cite the page, not a region, and say so.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Optional

from PIL import Image

from citadel_gateway import Gateway

from citadel_knowledge.extract import to_jpeg
from citadel_knowledge.normalise import Block

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["document_type", "summary", "stamps", "signatures", "handwritten_fields", "key_values"],
    "properties": {
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "stamps": {
            "type": "array",
            "items": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
        },
        "signatures": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["present_near"],
                "properties": {"present_near": {"type": "string"}},
            },
        },
        "handwritten_fields": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field", "value"],
                "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
            },
        },
        "key_values": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "value"],
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            },
        },
    },
}

VISION_PROMPT = (
    "You are reading ONE page image from an industrial plant's engineering records. "
    "Report only what is visibly on the page; never guess. Return JSON with: "
    "document_type (e.g. inspection report, calibration record, nameplate); "
    "summary (one sentence); stamps (text of each rubber stamp or seal, if any); "
    "signatures (for each handwritten signature, the printed name or label it is next to); "
    "handwritten_fields (handwritten entries in form fields, if any); "
    "key_values (up to 12 important labelled values such as equipment tag, dates, "
    "pressures, thicknesses, exactly as written). Use empty lists when there are none."
)

#: The vision model sees a downscaled page -- enough to read a stamp, small enough to
#: keep the prompt inside a small model's context.
_MAX_SIDE = 1344


def read_page(
    gateway: Gateway,
    image: Image.Image,
    *,
    classification: str,
    actor_id: Optional[str],
) -> dict[str, Any]:
    encoded = base64.b64encode(to_jpeg(image, max_side=_MAX_SIDE, quality=85)).decode("ascii")
    result = gateway.vision(
        VISION_PROMPT,
        [encoded],
        classification=classification,
        schema=VISION_SCHEMA,
        purpose="vision.ingest",
        actor_id=actor_id,
    )
    reading: dict[str, Any] = dict(result.data) if isinstance(result.data, Mapping) else {}
    reading["status"] = "read"
    reading["model_id"] = result.model_id
    reading["fallback_used"] = result.fallback_used
    return reading


def vision_blocks(reading: Mapping[str, Any]) -> list[Block]:
    """Turn a page reading into citable blocks (page-level, no region)."""
    blocks: list[Block] = []
    summary = str(reading.get("summary") or "").strip()
    doc_type = str(reading.get("document_type") or "").strip()
    if summary or doc_type:
        blocks.append(Block("figure", "vision", f"Page reading ({doc_type or 'document'}): {summary}".strip()))
    for stamp in reading.get("stamps") or []:
        text = str(stamp.get("text") if isinstance(stamp, Mapping) else stamp).strip()
        if text:
            blocks.append(Block("stamp", "vision", f"Stamp on page: {text}"))
    for signature in reading.get("signatures") or []:
        near = str(signature.get("present_near") if isinstance(signature, Mapping) else signature).strip()
        blocks.append(Block("signature", "vision", f"Handwritten signature present near: {near or 'unlabelled'}"))
    for field in reading.get("handwritten_fields") or []:
        if isinstance(field, Mapping) and str(field.get("value") or "").strip():
            blocks.append(Block("field", "vision", f"Handwritten {field.get('field')}: {field.get('value')}"))
    pairs = [
        f"{kv.get('key')}: {kv.get('value')}"
        for kv in reading.get("key_values") or []
        if isinstance(kv, Mapping) and str(kv.get("value") or "").strip()
    ]
    if pairs:
        blocks.append(Block("field", "vision", "Values read from the page image: " + "; ".join(pairs)))
    return blocks


__all__ = ["VISION_SCHEMA", "VISION_PROMPT", "read_page", "vision_blocks"]
