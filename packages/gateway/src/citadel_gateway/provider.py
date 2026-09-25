"""The `InferenceProvider` seam (ADR-0001 §Q3, ADR-0002 §2).

Written before either implementation and carried by both: streaming, JSON-schema
structured output, a residency query, load/pin/evict (or a documented no-op), per-call
model override (every call names its tag), and token accounting on every response.
Where a runtime cannot honour one, `supports()` says so and the gateway records the
degradation rather than working around it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence, runtime_checkable

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
    """The form two model names are compared in -- never the form sent to a runtime.

    Ollama reports an untagged model as `name:latest`; the registry may omit it. Ollama
    also answers to a name in any letter case, but lists a model the way it was pulled:
    after `ollama pull name:4B`, /api/tags says `name:4B`, and the registry's `name:4b`
    has to find it. (It did not, once, on the demonstration machine: every plan step
    failed with "not installed on the runtime" while Ollama had the model.) So the
    comparison ignores case. A request keeps the runtime's own spelling; see
    `RuntimeSpellings`."""
    folded = tag.strip().lower()
    return folded if ":" in folded.split("/")[-1] else f"{folded}:latest"


class RuntimeSpellings:
    """The names a runtime listed, by their compared form: requests go out under the
    runtime's own spelling, which a case-sensitive runtime needs. A name the runtime has
    not listed (yet) goes out as given."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}

    def remember(self, names: Iterable[str]) -> None:
        self._by_key = {normalise_tag(name): name for name in names}

    def __call__(self, tag: str) -> str:
        return self._by_key.get(normalise_tag(tag), tag)


__all__ = ["InferenceProvider", "ProgressCallback", "RuntimeSpellings", "normalise_tag"]
