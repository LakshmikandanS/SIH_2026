"""`InferenceProvider` for Ollama -- the `demo-local` runtime (ADR-0001 §Q3).

Ollama already implements the residency manager ADR-0001 calls the biggest technical
risk in the project: a cap on loaded models, queue-when-full, no CPU spill, keep-alive
pinning. This provider *drives* that machinery -- it reads `/api/ps` for the truth
about what is loaded and passes `keep_alive` per request -- and never runs a second
scheduler beside it.

Plain HTTP via httpx, deliberately not the `ollama` Python client: one fewer
dependency to vendor for the offline bundle, and every byte this process sends to the
runtime is visible in this file.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator, Mapping, Optional, Sequence

import httpx

from citadel_gateway.provider import ProgressCallback, RuntimeSpellings, normalise_tag
from citadel_gateway.types import (
    InstalledModel,
    LoadedModel,
    Message,
    ProviderError,
    ProviderResponse,
    ProviderSupport,
    Usage,
)


def _message_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.images:
        payload["images"] = list(message.images)
    return payload


class OllamaProvider:
    name = "ollama"

    def __init__(self, endpoint: str, *, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        # trust_env=False: never route through an HTTP(S)_PROXY / ALL_PROXY from the environment
        # (Docker Desktop injects one into containers whenever a proxy is configured). The
        # inference endpoint is on-box by design; honouring an ambient proxy would carry every
        # prompt and document excerpt OFF the box -- the one thing this system exists not to do.
        self._client = httpx.Client(
            base_url=self.endpoint,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=10.0),
            transport=transport,
            trust_env=False,
        )
        self._capabilities: dict[str, frozenset[str]] = {}
        # Requests name a model the way /api/tags listed it (see normalise_tag).
        self._spelling = RuntimeSpellings()

    # -- plumbing ---------------------------------------------------------------

    def _post(self, path: str, body: Mapping[str, Any], *, timeout_s: float = 300.0) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=dict(body), timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"ollama {path} timed out after {timeout_s:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama unreachable at {self.endpoint}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"ollama {path} returned {response.status_code}: {_error_text(response)}",
                retryable=response.status_code >= 500,
            )
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(path, timeout=10.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama unreachable at {self.endpoint}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"ollama {path} returned {response.status_code}: {_error_text(response)}")
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def model_capabilities(self, tag: str) -> frozenset[str]:
        """What the runtime says this model can do (`/api/show`), cached. Used for one
        decision only: whether to send `think: false` to a reasoning model."""
        key = normalise_tag(tag)
        if key not in self._capabilities:
            try:
                shown = self._post("/api/show", {"model": self._spelling(tag)}, timeout_s=15.0)
                caps = shown.get("capabilities")
                self._capabilities[key] = frozenset(c for c in caps if isinstance(c, str)) if isinstance(caps, list) else frozenset()
            except ProviderError:
                self._capabilities[key] = frozenset()
        return self._capabilities[key]

    # -- InferenceProvider --------------------------------------------------------

    def supports(self) -> ProviderSupport:
        return ProviderSupport()

    def version(self) -> Optional[str]:
        try:
            value = self._get("/api/version").get("version")
        except ProviderError:
            return None
        return str(value) if value else None

    def installed(self) -> list[InstalledModel]:
        models = self._get("/api/tags").get("models") or []
        found: list[InstalledModel] = []
        names: list[str] = []
        for entry in models:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
                found.append(
                    InstalledModel(
                        tag=normalise_tag(entry["name"]),
                        size_bytes=entry.get("size") if isinstance(entry.get("size"), int) else None,
                        digest=entry.get("digest") if isinstance(entry.get("digest"), str) else None,
                    )
                )
        self._spelling.remember(names)
        return found

    def loaded(self) -> list[LoadedModel]:
        models = self._get("/api/ps").get("models") or []
        found: list[LoadedModel] = []
        for entry in models:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                found.append(
                    LoadedModel(
                        tag=normalise_tag(entry["name"]),
                        size_bytes=entry.get("size") if isinstance(entry.get("size"), int) else None,
                        vram_bytes=entry.get("size_vram") if isinstance(entry.get("size_vram"), int) else None,
                        expires_at=entry.get("expires_at") if isinstance(entry.get("expires_at"), str) else None,
                    )
                )
        return found

    def _chat_body(
        self,
        tag: str,
        messages: Sequence[Message],
        *,
        stream: bool,
        schema: Optional[Mapping[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        context_window: Optional[int],
        keep_alive: Optional[str],
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if context_window is not None:
            options["num_ctx"] = context_window
        body: dict[str, Any] = {
            "model": self._spelling(tag),
            "messages": [_message_payload(m) for m in messages],
            "stream": stream,
            "options": options,
        }
        if schema is not None:
            body["format"] = dict(schema)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        # Reasoning models spend their budget on hidden deliberation unless told not
        # to; the gateway wants the answer, and structured output wants it clean.
        if "thinking" in self.model_capabilities(tag):
            body["think"] = False
        return body

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
    ) -> ProviderResponse:
        body = self._chat_body(
            tag,
            messages,
            stream=False,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            context_window=context_window,
            keep_alive=keep_alive,
        )
        started = time.perf_counter()
        reply = self._post("/api/chat", body, timeout_s=timeout_s)
        latency = int((time.perf_counter() - started) * 1000)
        message = reply.get("message") if isinstance(reply.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        return ProviderResponse(
            text=content if isinstance(content, str) else "",
            usage=Usage(
                prompt_tokens=int(reply.get("prompt_eval_count") or 0),
                completion_tokens=int(reply.get("eval_count") or 0),
            ),
            latency_ms=latency,
            raw_model=str(reply.get("model") or tag),
        )

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
    ) -> Iterator[tuple[str, Optional[Usage]]]:
        body = self._chat_body(
            tag,
            messages,
            stream=True,
            schema=None,
            temperature=temperature,
            max_tokens=max_tokens,
            context_window=context_window,
            keep_alive=keep_alive,
        )
        try:
            with self._client.stream("POST", "/api/chat", json=body, timeout=timeout_s) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(f"ollama /api/chat returned {response.status_code}: {_error_text(response)}")
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ProviderError(f"ollama stream error: {chunk['error']}")
                    message = chunk.get("message") or {}
                    delta = message.get("content") or ""
                    if chunk.get("done"):
                        yield delta, Usage(
                            prompt_tokens=int(chunk.get("prompt_eval_count") or 0),
                            completion_tokens=int(chunk.get("eval_count") or 0),
                        )
                        return
                    if delta:
                        yield delta, None
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama stream failed: {exc}") from exc

    def embed(self, tag: str, texts: Sequence[str], *, keep_alive: Optional[str] = None) -> tuple[list[list[float]], Usage]:
        body: dict[str, Any] = {"model": self._spelling(tag), "input": list(texts)}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        try:
            reply = self._post("/api/embed", body, timeout_s=300.0)
        except ProviderError as exc:
            if "404" not in str(exc):
                raise
            # Pre-/api/embed runtimes: one text per call on the legacy endpoint.
            vectors = []
            for text in texts:
                legacy = self._post("/api/embeddings", {"model": self._spelling(tag), "prompt": text}, timeout_s=120.0)
                vectors.append([float(v) for v in legacy.get("embedding") or []])
            return vectors, Usage()
        raw = reply.get("embeddings") or []
        vectors = [[float(v) for v in row] for row in raw if isinstance(row, list)]
        if len(vectors) != len(texts):
            raise ProviderError(f"ollama returned {len(vectors)} embeddings for {len(texts)} inputs")
        return vectors, Usage(prompt_tokens=int(reply.get("prompt_eval_count") or 0))

    def pull(self, tag: str, progress: ProgressCallback) -> None:
        try:
            with self._client.stream("POST", "/api/pull", json={"model": tag, "stream": True}, timeout=None) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(f"ollama pull {tag} returned {response.status_code}: {_error_text(response)}")
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    update = json.loads(line)
                    if "error" in update:
                        raise ProviderError(f"ollama pull {tag}: {update['error']}", retryable=False)
                    progress(update)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama pull {tag} failed: {exc}") from exc

    def set_keep_alive(self, tag: str, keep_alive: str) -> None:
        """Load-and-pin (`-1`), or evict (`0`), with an empty generate request -- the
        documented way to change residency without generating anything."""
        self._post("/api/generate", {"model": self._spelling(tag), "keep_alive": keep_alive}, timeout_s=300.0)


def _error_text(response: httpx.Response) -> str:
    try:
        parsed = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(parsed, dict) and "error" in parsed:
        return str(parsed["error"])[:300]
    return response.text[:300]


__all__ = ["OllamaProvider"]
