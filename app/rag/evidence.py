"""Evidence -- design doc section 3, owned by the Data Plane.

    {"evidence_id": "E001", "document_id": "DOC-P101-HIST", "document_version": "1",
     "page": 4, "text": "...", "classification": "CONFIDENTIAL",
     "acl": ["maintenance", "engineering"], "provenance_id": "E001"}

Section 6.9's provenance story for this slice: "not a separate graph store --
`provenance_id` on an evidence row IS that row's own primary key." So
`provenance_id` is never anything other than `evidence_id`, always, and there
is nothing else to build for multi-hop traversal (explicitly deferred).

`Evidence.from_chunk` is the only bridge from this package's internal
`app.rag.store.Chunk` to the object that actually crosses the Tool Gateway
boundary, and it is called exactly once per chunk that survives
`app.rag.search`'s ACL/classification filter -- never for a denied one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from app import ids

if TYPE_CHECKING:
    from app.rag.store import Chunk


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    document_id: str
    document_version: str
    page: int
    text: str
    classification: str
    acl: tuple[str, ...]
    provenance_id: str
    #: Not part of section 3's schema -- an extra transparency field so the
    #: result list's ranking is inspectable (data/README.md: "ranking is
    #: meaningful"). Omitted from `to_dict()` when absent, so a caller that
    #: built an `Evidence` some other way still gets exactly the contract
    #: shape.
    score: Optional[float] = None

    @classmethod
    def from_chunk(cls, chunk: "Chunk", *, score: Optional[float] = None) -> "Evidence":
        evidence_id = ids.new_id(ids.EVIDENCE)
        return cls(
            evidence_id=evidence_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            page=chunk.page,
            text=chunk.text,
            classification=chunk.classification,
            acl=tuple(chunk.acl),
            provenance_id=evidence_id,
            score=score,
        )

    def to_dict(self) -> dict[str, Any]:
        """The design doc section 3 / section 6.9 wire shape, exactly."""
        payload: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "page": self.page,
            "text": self.text,
            "classification": self.classification,
            "acl": list(self.acl),
            "provenance_id": self.provenance_id,
        }
        if self.score is not None:
            payload["score"] = round(self.score, 6)
        return payload
