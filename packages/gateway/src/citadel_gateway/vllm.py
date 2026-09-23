"""`InferenceProvider` for vLLM's OpenAI-compatible server -- the `hpc-eval` runtime.

ADR-0002: an interface with one implementation is a guess, so the second one ships
alongside the first. vLLM holds every model resident for its whole lifetime, so the
residency query answers honestly -- whatever the server lists is loaded -- and pull /
pin / evict are declared unsupported in `supports()` rather than faked.

The endpoint is not in the registry for this profile: SLURM allocates a node per job
and the sbatch script writes the URL to `endpoint_file` (ops/hpc/README.md). The
gateway resolves that before constructing this provider.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator, Mapping, Optional, Sequence

import httpx

from citadel_gateway.provider import ProgressCallback
from citadel_gateway.types import (
    InstalledModel,
    LoadedModel,
    Message,
    ProviderError,
    ProviderResponse,
    ProviderSupport,
    Usage,
)


def _content(message: Message) -> Any:
    if not message.images:
        return message.content
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}})
    return parts


class VLLMProvider:
    name = "vllm"

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

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None, *, timeout_s: float = 300.0) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=dict(body) if body is not None else None, timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise ProviderError(f"vllm unreachable at {self.endpoint}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"vllm {path} returned {response.status_code}: {response.text[:300]}")
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def supports(self) -> ProviderSupport:
        return ProviderSupport(pin_evict=False, pull=False)

    def version(self) -> Optional[str]:
        try:
            value = self._request("GET", "/version", timeout_s=10.0).get("version")
        except ProviderError:
            return None
        return str(value) if value else None

    def installed(self) -> list[InstalledModel]:
        data = self._request("GET", "/v1/models", timeout_s=10.0).get("data") or []
        return [InstalledModel(tag=str(m["id"])) for m in data if isinstance(m, dict) and "id" in m]

    def loaded(self) -> list[LoadedModel]:
        # Everything a vLLM server serves is resident for the server's lifetime.
        return [LoadedModel(tag=m.tag) for m in self.installed()]

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
        body: dict[str, Any] = {
            "model": tag,
            "messages": [{"role": m.role, "content": _content(m)} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": dict(schema)},
            }
        started = time.perf_counter()
        reply = self._request("POST", "/v1/chat/completions", body, timeout_s=timeout_s)
        latency = int((time.perf_counter() - started) * 1000)
        choices = reply.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "")
        usage = reply.get("usage") or {}
        return ProviderResponse(
            text=text,
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
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
        body: dict[str, Any] = {
            "model": tag,
            "messages": [{"role": m.role, "content": _content(m)} for m in messages],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            with self._client.stream("POST", "/v1/chat/completions", json=body, timeout=timeout_s) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(f"vllm stream returned {response.status_code}: {response.text[:300]}")
                usage: Optional[Usage] = None
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield str(delta), None
                yield "", usage or Usage()
        except httpx.HTTPError as exc:
            raise ProviderError(f"vllm stream failed: {exc}") from exc

    def embed(self, tag: str, texts: Sequence[str], *, keep_alive: Optional[str] = None) -> tuple[list[list[float]], Usage]:
        reply = self._request("POST", "/v1/embeddings", {"model": tag, "input": list(texts)})
        data = sorted((d for d in reply.get("data") or [] if isinstance(d, dict)), key=lambda d: int(d.get("index", 0)))
        usage = reply.get("usage") or {}
        return [[float(v) for v in d.get("embedding") or []] for d in data], Usage(int(usage.get("prompt_tokens") or 0))

    def pull(self, tag: str, progress: ProgressCallback) -> None:
        raise ProviderError("vllm serves a fixed model set chosen at launch; there is nothing to pull", retryable=False)

    def set_keep_alive(self, tag: str, keep_alive: str) -> None:
        # Documented no-op: everything is resident for the server's lifetime.
        return None


__all__ = ["VLLMProvider"]
