"""Chunk-level splitting for the vector store (design doc section 6.9).

The corpus's own README is explicit about why this matters: whole-document
embedding near-ties `pump_p101_history.txt` and `pump_p101_spec_sheet.txt`
(both are about the same asset), while chunk-level retrieval against a
specific query separates them cleanly. The Evidence schema also has a `page`
field to fill in -- returning one giant blob per document both retrieves
poorly and leaves that field meaningless.

The source corpus is plain `.txt`, not `.pdf` (data/README.md), so there are
no real page boundaries to read. `page` is mapped honestly here: it is the
1-based ordinal position of the chunk within its document, in reading order --
a stand-in for pagination, not a lie about one.
"""

from __future__ import annotations

import re

#: A decorative rule made only of dashes ("--------..."), the corpus's own
#: section-divider convention. It carries no retrievable meaning and would
#: otherwise survive as a nearly-content-free chunk on its own.
_DECORATIVE_RULE = re.compile(r"^-{5,}$")

#: Paragraphs are blank-line delimited -- one or more newlines with only
#: whitespace between them.
_BLANK_LINE = re.compile(r"\n\s*\n")


def chunk_text(text: str, *, min_chunk_chars: int = 40) -> list[str]:
    """Split one document's text into retrieval chunks.

    1. Split on blank lines, so a chunk is a paragraph or a short section
       (the corpus's actual structure -- see data/maintenance/*.txt).
    2. Strip pure decorative-rule lines out of each paragraph, so a
       divider line does not itself become "content".
    3. Merge any paragraph shorter than `min_chunk_chars` into the next one,
       so a bare heading ("MAINTENANCE LIMITS") is never embedded alone --
       it carries no retrievable signal by itself and would just add noise.

    Returns chunks in reading order; the caller assigns `page` as
    `enumerate(..., start=1)`.
    """
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []

    cleaned: list[str] = []
    for paragraph in _BLANK_LINE.split(normalized):
        kept_lines = [
            line for line in paragraph.splitlines() if not _DECORATIVE_RULE.match(line.strip())
        ]
        candidate = "\n".join(kept_lines).strip()
        if candidate:
            cleaned.append(candidate)

    if not cleaned:
        return [normalized]

    merged: list[str] = []
    buffer = ""
    for paragraph in cleaned:
        buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(buffer) >= min_chunk_chars:
            merged.append(buffer)
            buffer = ""

    if buffer:
        if merged:
            merged[-1] = f"{merged[-1]}\n\n{buffer}"
        else:
            merged.append(buffer)

    return merged
