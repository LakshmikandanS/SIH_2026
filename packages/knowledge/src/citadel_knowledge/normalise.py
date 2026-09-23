"""The normalised document: pages of blocks, each with a box and a content hash.

Whatever the source -- a born-digital text layer, OCR of a scan, or what the vision model
read off a page -- ingestion reduces it to this one shape before anything is chunked,
embedded or cited. A block's `bbox` is (x0, y0, x1, y1) normalised to [0, 1] of the
page, the same convention as `citadel_contracts.domain.Evidence.bbox`, so the UI can
highlight the exact region regardless of the resolution it renders the page at.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

BBox = tuple[float, float, float, float]

#: Below this mean OCR confidence a block is flagged for the reader rather than
#: silently trusted (ADR-0001 §Q8: low-confidence regions are flagged as such).
LOW_CONFIDENCE = 60.0


@dataclass
class Block:
    kind: str
    source: str
    text: str
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None

    @property
    def low_confidence(self) -> bool:
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class PageContent:
    page: int
    text_source: str  # text_layer | ocr | none
    blocks: list[Block] = field(default_factory=list)
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    image_ref: Optional[str] = None
    ocr_confidence: Optional[float] = None
    vision: Optional[dict[str, Any]] = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def clamp_bbox(x0: float, y0: float, x1: float, y1: float) -> BBox:
    def c(v: float) -> float:
        return round(min(1.0, max(0.0, v)), 4)

    return (c(min(x0, x1)), c(min(y0, y1)), c(max(x0, x1)), c(max(y0, y1)))


def union_bbox(boxes: Iterable[Optional[BBox]]) -> Optional[BBox]:
    present = [b for b in boxes if b is not None]
    if not present:
        return None
    return clamp_bbox(
        min(b[0] for b in present),
        min(b[1] for b in present),
        max(b[2] for b in present),
        max(b[3] for b in present),
    )


def mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


__all__ = ["BBox", "Block", "PageContent", "LOW_CONFIDENCE", "clamp_bbox", "union_bbox", "mean"]
