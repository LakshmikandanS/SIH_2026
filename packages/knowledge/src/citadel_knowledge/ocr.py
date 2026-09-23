"""OCR on the application CPU (ADR-0001 §Q2): Tesseract, with word boxes kept.

Tesseract reports every word with a box and a confidence. Words are grouped by the
paragraph Tesseract itself detected, so each block keeps a real region of the page to
cite, and a paragraph whose mean confidence is low is flagged rather than trusted
(ADR-0001 §Q8, "degrade honestly").
"""

from __future__ import annotations

import shutil
from typing import Any

from PIL import Image

from citadel_knowledge.normalise import Block, clamp_bbox, mean


class OCRUnavailable(RuntimeError):
    """Tesseract is not installed on this machine. Recorded as a degradation for the
    page -- the page keeps its image and its vision reading, just no OCR text."""


def available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_blocks(image: Image.Image, *, language: str = "eng") -> tuple[list[Block], float | None]:
    if not available():
        raise OCRUnavailable("tesseract is not on PATH")
    import pytesseract

    data: dict[str, list[Any]] = pytesseract.image_to_data(
        image, lang=language, config="--psm 3", output_type=pytesseract.Output.DICT
    )
    width, height = image.size
    groups: dict[tuple[int, int], dict[int, list[int]]] = {}
    for i, text in enumerate(data["text"]):
        if not str(text).strip():
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]))
        groups.setdefault(key, {}).setdefault(int(data["line_num"][i]), []).append(i)

    blocks: list[Block] = []
    confidences: list[float] = []
    for key in sorted(groups, key=lambda k: min(int(data["top"][i]) for idx in groups[k].values() for i in idx)):
        lines = groups[key]
        line_texts = []
        indices: list[int] = []
        for line_no in sorted(lines):
            words = sorted(lines[line_no], key=lambda i: int(data["left"][i]))
            line_texts.append(" ".join(str(data["text"][i]).strip() for i in words))
            indices.extend(words)
        word_conf = [float(data["conf"][i]) for i in indices if float(data["conf"][i]) >= 0]
        x0 = min(int(data["left"][i]) for i in indices)
        y0 = min(int(data["top"][i]) for i in indices)
        x1 = max(int(data["left"][i]) + int(data["width"][i]) for i in indices)
        y1 = max(int(data["top"][i]) + int(data["height"][i]) for i in indices)
        block_conf = mean(word_conf)
        if block_conf is not None:
            confidences.append(block_conf)
        blocks.append(
            Block(
                kind="text",
                source="ocr",
                text="\n".join(line_texts),
                bbox=clamp_bbox(x0 / width, y0 / height, x1 / width, y1 / height),
                confidence=block_conf,
            )
        )
    return blocks, mean(confidences)


__all__ = ["OCRUnavailable", "available", "ocr_blocks"]
