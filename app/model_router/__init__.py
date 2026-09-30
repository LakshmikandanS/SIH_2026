"""Model Router (design doc section 6.3, C-003, BB-006, BB-007, BB-008, BB-009).

One canonical request/response schema for every model call in this slice.
Selection is a static lookup table (`app.model_router.router.route_models`),
never a scoring formula; the actual reasoning-model HTTP call
(`app.model_router.reasoning_client.generate_json`) is a separate, thin
client `app.orchestrator.plan` uses once the model to call has been chosen.
"""

from app.model_router.errors import (
    MODEL_CLASSIFICATION_INCOMPATIBLE,
    NO_MODEL_FOR_CAPABILITY,
    REASONING_CALL_FAILED,
    ModelRoutingError,
)
from app.model_router.manifest import MODEL_MANIFEST, ModelManifestEntry
from app.model_router.reasoning_client import ReasoningModelError, generate_json
from app.model_router.router import ModelRouterResponse, route_models

__all__ = [
    "MODEL_CLASSIFICATION_INCOMPATIBLE",
    "MODEL_MANIFEST",
    "NO_MODEL_FOR_CAPABILITY",
    "REASONING_CALL_FAILED",
    "ModelManifestEntry",
    "ModelRouterResponse",
    "ModelRoutingError",
    "ReasoningModelError",
    "generate_json",
    "route_models",
]
