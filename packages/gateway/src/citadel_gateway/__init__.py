"""The Model Gateway: registry-driven routing with a visible score breakdown,
residency, health, honest fallback, admission, and the InferenceProvider seam."""

from __future__ import annotations

from citadel_gateway.admission import AdmissionGate
from citadel_gateway.gateway import ENDPOINT_OVERRIDE_VAR, Gateway, build_provider
from citadel_gateway.jsonutil import StructuredOutputError, parse_json, parse_structured
from citadel_gateway.provider import InferenceProvider, normalise_tag
from citadel_gateway.router import RuntimeView, route, score_model
from citadel_gateway.types import (
    CandidateScore,
    EmbeddingResult,
    GenerationResult,
    Message,
    NoEligibleModel,
    ProviderError,
    RoutingDecision,
    RoutingRequest,
    ScoreTerm,
    Usage,
    chat_messages,
)

__all__ = [
    "AdmissionGate",
    "Gateway",
    "build_provider",
    "ENDPOINT_OVERRIDE_VAR",
    "InferenceProvider",
    "normalise_tag",
    "RuntimeView",
    "route",
    "score_model",
    "StructuredOutputError",
    "parse_json",
    "parse_structured",
    "CandidateScore",
    "EmbeddingResult",
    "GenerationResult",
    "Message",
    "NoEligibleModel",
    "ProviderError",
    "RoutingDecision",
    "RoutingRequest",
    "ScoreTerm",
    "Usage",
    "chat_messages",
]
