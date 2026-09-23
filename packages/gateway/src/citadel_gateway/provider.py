"""The `InferenceProvider` seam (ADR-0001 §Q3, ADR-0002 §2).

Written before either implementation and carried by both: streaming, JSON-schema
structured output, a residency query, load/pin/evict (or a documented no-op), per-call
model override (every call names its tag), and token accounting on every response.
Where a runtime cannot honour one, `supports()` says so and the gateway records the
degradation rather than working around it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, Sequence, runtime_checkable

from citadel_gateway.types import (
    InstalledModel,
    LoadedModel,
    Message,
    ProviderResponse,
    ProviderSupport,
    Usage,
)

ProgressCallback = Callable[[Mapping[str, Any]], None]


@runtime_checkable
class InferenceProvider(Protocol):
    name: str
    endpoint: str

    def supports(self) -> ProviderSupport: ...

    def version(self) -> Optional[str]: ...

    def installed(self) -> list[InstalledModel]: ...

    def loaded(self) -> list[LoadedModel]: ...

    def chat(
        self,
        tag: str,
        messages: Sequence[Message],
        *,
        schema: Optional[Mapping[str, Any]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        keep_alive: Optional[str] = None,
        timeout_s: float = 300.0,
    ) -> ProviderResponse: ...

    def stream_chat(
        self,
        tag: str,
        messages: Sequence[Message],
        *,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        keep_alive: Optional[str] = None,
        timeout_s: float = 300.0,
    ) -> Iterator[tuple[str, Optional[Usage]]]: ...

    def embed(self, tag: str, texts: Sequence[str], *, keep_alive: Optional[str] = None) -> tuple[list[list[float]], Usage]: ...

    def pull(self, tag: str, progress: ProgressCallback) -> None: ...

    def set_keep_alive(self, tag: str, keep_alive: str) -> None: ...


def normalise_tag(tag: str) -> str:
    """Ollama reports an untagged model as `name:latest`; the registry may omit it."""
    return tag if ":" in tag.split("/")[-1] else f"{tag}:latest"


__all__ = ["InferenceProvider", "ProgressCallback", "normalise_tag"]
