"""Layout-aware chunking: whole blocks, never across a page.

A chunk is the unit retrieval ranks and a citation points at, so two rules hold:
chunks are built from whole blocks (a sentence is never cut mid-way to hit a size), and
a chunk never spans two pages (a citation is a page and a region, and a region cannot
be on two pages). A chunk's box is the union of its blocks' boxes; vision findings,
which have no box, are chunked separately so they never widen a text chunk's region to
the whole page.
"""

from __future__ import annotations

from dataclasses import dataclass

from citadel_knowledge.normalise import BBox, PageContent, union_bbox

MAX_CHARS = 900


@dataclass(frozen=True)
class Chunk:
    page: int
    index: int
    text: str
    bbox: BBox | None
    block_indexes: tuple[int, ...]


def chunk_page(page: PageContent, *, start_index: int, max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for source_group in ("layout", "vision"):
        members = [
            (i, b)
            for i, b in enumerate(page.blocks)
            if b.text.strip() and ((b.source == "vision") == (source_group == "vision"))
        ]
        current: list[int] = []
        size = 0
        for index, block in members:
            if current and size + len(block.text) > max_chars:
                chunks.append(_make(page, current, start_index + len(chunks)))
                current, size = [], 0
            current.append(index)
            size += len(block.text) + 1
        if current:
            chunks.append(_make(page, current, start_index + len(chunks)))
    return chunks


def _make(page: PageContent, indexes: list[int], chunk_index: int) -> Chunk:
    blocks = [page.blocks[i] for i in indexes]
    return Chunk(
        page=page.page,
        index=chunk_index,
        text="\n".join(b.text for b in blocks),
        bbox=union_bbox(b.bbox for b in blocks),
        block_indexes=tuple(indexes),
    )


__all__ = ["Chunk", "chunk_page", "MAX_CHARS"]
