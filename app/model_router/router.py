"""The Model Router (design doc section 6.3, C-003, BB-006, BB-007, BB-009).

    "One canonical schema, used for both the planning call and any other
     model call in this slice [...] Selection logic (BB-006, deliberately
     trivial): a static lookup table, not a scoring formula --

        classification requires local-only  ->  reasoning: qwen3-local
        embedding always needed              ->  embedding: bge-base-local

     Model availability (BB-008): no health-check, no fallback chain
     population (`fallback_chain` stays `[]`)."

`route_models` below is exactly that lookup, nothing more: for every
requested capability (plus embedding, which is always needed regardless of
what was asked for) there is exactly one manifest entry, and the one check
that can fail it is the classification ceiling (BB-007). There is no
scoring, no ranking among candidates, no health probe -- the whole function
is a dict lookup and one comparison per capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.db.state_machines import Classification
from app.model_router.errors import (
    MODEL_CLASSIFICATION_INCOMPATIBLE,
    NO_MODEL_FOR_CAPABILITY,
    ModelRoutingError,
)
from app.model_router.manifest import MODEL_MANIFEST, ModelManifestEntry

#: This slice's Model Router only ever needs to satisfy these two
#: capabilities; embedding is folded in unconditionally below regardless of
#: what a caller asks for, matching section 6.3's own wording ("embedding
#: always needed").
_ALWAYS_NEEDED: tuple[str, ...] = ("embedding",)


@dataclass(frozen=True)
class ModelRouterResponse:
    """The ONE response shape of section 6.3 -- the older single-model shape
    elsewhere in docs/architecture/ is retired for this slice."""

    selected_models: dict[str, str]
    routing_reason: str
    fallback_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_models": dict(self.selected_models),
            "routing_reason": self.routing_reason,
            "fallback_chain": list(self.fallback_chain),
        }


def route_models(
    *,
    task_id: str,
    required_capabilities: Sequence[str],
    classification: str,
    manifest: Mapping[str, ModelManifestEntry] = MODEL_MANIFEST,
) -> ModelRouterResponse:
    """section 6.3's request/response, as a direct function call rather than
    an HTTP round trip -- Model Router lives in the same trusted-zone process
    as the Orchestrator (design doc section 2), so there is no network hop
    here any more than there is one to the Policy Engine (section 6.6).

    Raises `ModelRoutingError` (never returns a partial selection) when:
      * a requested capability has no manifest entry at all
        (`NO_MODEL_FOR_CAPABILITY` -- defensive only; the real manifest
        always covers both capabilities this slice needs), or
      * the task's classification exceeds the one candidate's
        `max_classification` (`MODEL_CLASSIFICATION_INCOMPATIBLE`, BB-007).
    """
    # Preserves caller order, de-duplicates, and folds in the capabilities
    # that are always needed -- a dict used purely as an ordered set.
    capabilities_needed = list(
        dict.fromkeys([*required_capabilities, *_ALWAYS_NEEDED])
    )

    selected: dict[str, str] = {}
    for capability in capabilities_needed:
        candidates = [m for m in manifest.values() if m.capability == capability]
        if not candidates:
            raise ModelRoutingError(
                NO_MODEL_FOR_CAPABILITY,
                f"no model in the manifest offers capability {capability!r} "
                f"(task {task_id!r})",
            )
        # "a static lookup table, not a scoring formula" -- exactly one
        # candidate is expected per capability in this slice; the first (and
        # only) one is the selection.
        candidate = candidates[0]

        if Classification.exceeds(classification, candidate.max_classification):
            raise ModelRoutingError(
                MODEL_CLASSIFICATION_INCOMPATIBLE,
                f"task {task_id!r} classification {classification!r} exceeds "
                f"model {candidate.model_id!r}'s max_classification "
                f"{candidate.max_classification!r}",
            )
        selected[capability] = candidate.model_id

    primary = required_capabilities[0] if required_capabilities else _ALWAYS_NEEDED[0]
    routing_reason = f"local_{classification.lower()}_{primary}"

    return ModelRouterResponse(
        selected_models=selected,
        routing_reason=routing_reason,
        # BB-008: no fallback chain population, ever, for this slice.
        fallback_chain=[],
    )
