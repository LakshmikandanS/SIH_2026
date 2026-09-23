"""Template registry, generators, verification ladder, release.

Reached from `runtime` only through the `doc.generate` tool; never imported by it.
"""

from __future__ import annotations

from citadel_deliverables.content import Content, Item, Section, normalise
from citadel_deliverables.lifecycle import (
    MIME,
    ORG_NAME_VAR,
    ArtifactError,
    Generated,
    TaskFacts,
    build_provenance,
    cited_ids,
    decide,
    generate,
    get_artifact,
    read_bytes,
    store_artifact,
    transition,
)
from citadel_deliverables.render import SourceRef, rendered_text, to_html
from citadel_deliverables.verify import TierResult, Verification, is_grounded, quantities, verify

__all__ = [
    "Content",
    "Item",
    "Section",
    "normalise",
    "MIME",
    "ORG_NAME_VAR",
    "ArtifactError",
    "Generated",
    "TaskFacts",
    "build_provenance",
    "cited_ids",
    "decide",
    "generate",
    "get_artifact",
    "read_bytes",
    "store_artifact",
    "transition",
    "SourceRef",
    "rendered_text",
    "to_html",
    "TierResult",
    "Verification",
    "is_grounded",
    "quantities",
    "verify",
]
