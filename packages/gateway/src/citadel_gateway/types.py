"""The shapes that cross the gateway boundary.

Everything above the gateway -- knowledge, memory, tools, runtime, services -- talks in
these types and never in a runtime's own wire format. That is what lets one profile
run on Ollama and another on vLLM with nothing above this package noticing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str
    images: tuple[str, ...] = ()  # base64-encoded image bytes, for vision models


@dataclass(frozen=True)
class RoutingRequest:
    """What a caller needs from a model -- never which model it wants.

    `preferred_capability` is the task's dominant need (drafting vs code generation,
    say): it is what makes two task types route to two different models, which is
    acceptance target A. `classification` is the classification of the data that will
    be placed in the prompt; a model whose registry ceiling is below it is ineligible.
    """

    purpose: str
    required_capabilities: tuple[str, ...]
    modalities: tuple[str, ...] = ("text",)
    classification: str = "INTERNAL"
    context_estimate: int = 2048
    latency_budget_s: Optional[float] = None
    quality: str = "standard"
    preferred_capability: Optional[str] = None
    task_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "required_capabilities": list(self.required_capabilities),
            "modalities": list(self.modalities),
            "classification": self.classification,
            "context_estimate": self.context_estimate,
            "latency_budget_s": self.latency_budget_s,
            "quality": self.quality,
            "preferred_capability": self.preferred_capability,
        }


@dataclass(frozen=True)
class ScoreTerm:
    term: str
    points: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "points": self.points, "detail": self.detail}


@dataclass(frozen=True)
class CandidateScore:
    model_id: str
    tag: str
    eligible: bool
    total: float
    terms: tuple[ScoreTerm, ...]
    ineligible_because: tuple[str, ...] = ()
    resident: bool = False
    installed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "tag": self.tag,
            "eligible": self.eligible,
            "total": self.total,
            "terms": [t.to_dict() for t in self.terms],
            "ineligible_because": list(self.ineligible_because),
            "resident": self.resident,
            "installed": self.installed,
        }

    def summary(self) -> str:
        if not self.eligible:
            return f"{self.model_id}: ineligible ({'; '.join(self.ineligible_because)})"
        parts = " · ".join(
            f"{t.term} ({t.detail}) {t.points:+g}" if t.detail and t.term not in ("resident", "pinned set") else f"{t.term} {t.points:+g}"
            for t in self.terms
        )
        return f"{self.model_id}: {parts} = {self.total:g}"


@dataclass(frozen=True)
class RoutingDecision:
    """The selection, the fallback chain, and a score breakdown for EVERY candidate --
    kept on every response because the breakdown is the demonstration (target A)."""

    request: RoutingRequest
    selected: Optional[str]
    fallback_chain: tuple[str, ...]
    candidates: tuple[CandidateScore, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "selected": self.selected,
            "fallback_chain": list(self.fallback_chain),
            "candidates": [c.to_dict() for c in self.candidates],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ProviderResponse:
    """One raw call's outcome, before the gateway adds routing and fallback."""

    text: str
    usage: Usage
    latency_ms: int
    raw_model: str


@dataclass
class GenerationResult:
    text: str
    data: Any
    model_id: str
    tag: str
    routing: RoutingDecision
    usage: Usage
    latency_ms: int
    queue_wait_ms: int
    fallback_used: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def meta(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "tag": self.tag,
            "fallback_used": self.fallback_used,
            "degraded": list(self.degraded),
            "attempts": list(self.attempts),
            "usage": {"prompt": self.usage.prompt_tokens, "completion": self.usage.completion_tokens},
            "latency_ms": self.latency_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "routing": self.routing.to_dict(),
        }


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model_id: str
    tag: str
    dimensions: int
    latency_ms: int
    queue_wait_ms: int


@dataclass(frozen=True)
class InstalledModel:
    tag: str
    size_bytes: Optional[int] = None
    digest: Optional[str] = None


@dataclass(frozen=True)
class LoadedModel:
    tag: str
    size_bytes: Optional[int] = None
    vram_bytes: Optional[int] = None
    expires_at: Optional[str] = None


@dataclass(frozen=True)
class ProviderSupport:
    """What a runtime can honour. Where a flag is False the gateway records a
    degraded capability instead of working around it silently (ADR-0002 §2)."""

    streaming: bool = True
    structured_output: bool = True
    residency_query: bool = True
    pin_evict: bool = True
    pull: bool = True
    embeddings: bool = True
    vision: bool = True


class ProviderError(RuntimeError):
    """A runtime refused, failed or timed out. Carries enough to record honestly."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class NoEligibleModel(RuntimeError):
    def __init__(self, decision: RoutingDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


def chat_messages(system: Optional[str], user: str, images: Sequence[str] = ()) -> list[Message]:
    messages: list[Message] = []
    if system:
        messages.append(Message("system", system))
    messages.append(Message("user", user, tuple(images)))
    return messages


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "Message",
    "RoutingRequest",
    "ScoreTerm",
    "CandidateScore",
    "RoutingDecision",
    "Usage",
    "ProviderResponse",
    "GenerationResult",
    "EmbeddingResult",
    "InstalledModel",
    "LoadedModel",
    "ProviderSupport",
    "ProviderError",
    "NoEligibleModel",
    "chat_messages",
    "as_mapping",
]
