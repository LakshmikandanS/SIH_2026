"""The model manifest (design doc section 6.3, closing BB-007).

    "Model manifest gains exactly one new field to close BB-007:
     `"max_classification": "CONFIDENTIAL"`. A task whose classification
     exceeds a model's `max_classification` fails routing outright (`FAILED`,
     reason `MODEL_CLASSIFICATION_INCOMPATIBLE`)."

Two entries, matching section 6.3's own example response verbatim
(`{"reasoning": "qwen3-local", "embedding": "bge-base-local"}`) -- those are
the canonical *logical* model ids this slice's Model Router selects between
and returns to a caller. `ollama_model` is the one extra field this
implementation needs beyond the design doc's own schema: which real,
locally-serving Ollama model actually answers a call to that logical id.

Per docs/BUILD_LOG.md's Environment section (verified 2026-09-07):
`qwen3:4B` is a thinking model that returns an *empty* response under
`format=json` -- unusable for structured planning without extra handling
this slice does not build. `hermes3` is what was verified to produce
schema-valid plan JSON reliably, so it is what `qwen3-local` actually
resolves to here. The design doc's own naming is kept for the response
shape everything else (routing_reason, the response schema) is written
against; only the manifest's internal `ollama_model` field points at the
model that is actually loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.db.state_machines import Classification


@dataclass(frozen=True)
class ModelManifestEntry:
    model_id: str
    capability: str
    max_classification: str
    #: The real model name Ollama serves this logical id as (see module
    #: docstring). Never read outside `app.model_router`.
    ollama_model: str


MODEL_MANIFEST: Mapping[str, ModelManifestEntry] = {
    "qwen3-local": ModelManifestEntry(
        model_id="qwen3-local",
        capability="reasoning",
        max_classification=Classification.CONFIDENTIAL,
        ollama_model="hermes3",
    ),
    "bge-base-local": ModelManifestEntry(
        model_id="bge-base-local",
        capability="embedding",
        max_classification=Classification.CONFIDENTIAL,
        ollama_model="nomic-embed-text",
    ),
}
