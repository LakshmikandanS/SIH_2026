"""The Tool Gateway <-> Data Plane contract's filtering (design doc section 6.9).

    "Filter using both `requester.classification_max` (drop anything with a
     higher classification) and `requester.department` against each
     candidate document's `acl` (drop anything with no overlap). This
     mirrors the Policy Engine's own resource checks (`decide()`), but you
     must apply it here too."

`_passes` below is deliberately the same two comparisons as
`app.policy.engine._evaluate`'s two data rules -- same lattice
(`app.db.state_machines.Classification`, never string comparison), same
disjoint-set ACL test -- just evaluated against `requester.classification_max`
/ `requester.department` instead of a task row. That is not a coincidence:
the Policy Engine only decided that `rag.search` as an *operation* is allowed
in general; this module is what decides which specific documents may appear
in the result set, and it has to reach the same verdict the Policy Engine
would have reached had it been looking at this exact document.

The whole point of this module, structurally: a denied `Chunk` is never
turned into an `Evidence`, never scored, and never appears in
`SearchOutcome.results` or even in `SearchOutcome.filtered_documents` beyond
its `document_id`/`classification`/`acl` -- its `text` never leaves
`app.rag.store.Chunk`. Nothing "returns it and then drops it"; it is simply
never constructed into a result in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.db.state_machines import Classification
from app.rag import settings
from app.rag.embeddings import cosine_similarity, embed_text
from app.rag.evidence import Evidence
from app.rag.store import Chunk, VectorStore


@dataclass(frozen=True)
class Requester:
    """Design doc section 6.9's `requester` block, exactly.

    Built by the Tool Gateway from the *verified capability*
    (`ToolRequest.requester()`, app/tool_gateway/registry.py) -- never from
    agent-supplied arguments. This dataclass does not re-derive trust; it
    just gives the four fields a name inside this package.
    """

    task_id: Optional[str]
    agent_id: Optional[str]
    classification_max: str
    department: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Requester":
        return cls(
            task_id=data.get("task_id"),
            agent_id=data.get("agent_id"),
            classification_max=str(data["classification_max"]),
            department=str(data["department"]),
        )


@dataclass(frozen=True)
class DeniedDocument:
    """One document excluded from a search's candidate pool. Carries only
    what an auditor needs -- never `text` -- so the denial record itself
    cannot leak the content it is denying."""

    document_id: str
    classification: str
    acl: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "classification": self.classification,
            "acl": list(self.acl),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SearchOutcome:
    """Everything `app.rag.backend` needs, both for the `rag.search` result
    body and for the `EVIDENCE_RETRIEVED` audit event."""

    results: list[dict[str, Any]]
    total_candidate_chunks: int
    allowed_candidate_chunks: int
    filtered_documents: list[DeniedDocument]

    @property
    def filtered_chunk_count(self) -> int:
        return self.total_candidate_chunks - self.allowed_candidate_chunks

    @property
    def filtered_document_count(self) -> int:
        return len(self.filtered_documents)


def _passes(chunk: Chunk, requester: Requester) -> tuple[bool, str]:
    """The two section 6.9 checks, in the same order and against the same
    lattice as `app.policy.engine._evaluate`'s data rules."""
    try:
        exceeds = Classification.exceeds(chunk.classification, requester.classification_max)
    except ValueError as exc:
        # An unrecognised marking is not comparable, so it cannot be shown to
        # be within the requester's clearance. Fail closed, same as policy.
        return False, f"unknown_classification: {exc}"
    if exceeds:
        return False, "classification_exceeds_max"
    if set(chunk.acl).isdisjoint({requester.department}):
        return False, "acl_disjoint_from_department"
    return True, "ok"


def search(
    query: str,
    requester: Requester,
    *,
    store: VectorStore,
    top_k: Optional[int] = None,
    min_score: Optional[float] = None,
) -> SearchOutcome:
    """Rank `store`'s chunks against `query`, after dropping every chunk the
    requester is not entitled to see.

    Filtering happens strictly before ranking and strictly before an
    `Evidence` is ever built: a chunk that fails `_passes` is recorded (by
    document id only) in `filtered_documents` and then discarded -- it is
    never embedded into the scored candidate list, so a slow/expensive
    embedding call is never even wasted on content nobody is allowed to see
    (its embedding was already computed once, at ingestion time; nothing here
    needs to re-embed the chunk itself).
    """
    top_k = settings.TOP_K if top_k is None else top_k
    min_score = settings.MIN_SCORE if min_score is None else min_score

    chunks = store.chunks
    allowed: list[Chunk] = []
    denied_by_document: dict[str, DeniedDocument] = {}

    for chunk in chunks:
        ok, reason = _passes(chunk, requester)
        if not ok:
            # One denial record per document, not per chunk -- a document's
            # classification/acl are uniform across all of its chunks, so a
            # six-chunk document denied for one reason does not need six
            # identical audit lines.
            denied_by_document.setdefault(
                chunk.document_id,
                DeniedDocument(
                    document_id=chunk.document_id,
                    classification=chunk.classification,
                    acl=chunk.acl,
                    reason=reason,
                ),
            )
            continue
        allowed.append(chunk)

    query_vector = embed_text(query)
    scored = [(cosine_similarity(query_vector, chunk.embedding), chunk) for chunk in allowed]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results: list[dict[str, Any]] = []
    for score, chunk in scored:
        if score < min_score:
            break  # sorted descending: nothing after this clears the floor
        if len(results) >= top_k:
            break
        results.append(Evidence.from_chunk(chunk, score=score).to_dict())

    return SearchOutcome(
        results=results,
        total_candidate_chunks=len(chunks),
        allowed_candidate_chunks=len(allowed),
        filtered_documents=list(denied_by_document.values()),
    )
