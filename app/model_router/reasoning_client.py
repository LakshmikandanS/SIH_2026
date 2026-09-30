"""The reasoning-model HTTP client (design doc section 5.2, section 6.3).

Deliberately stdlib-only (`urllib.request`, `json`), mirroring
`app/rag/embeddings.py`'s own choice for the same reason: the corpus/call
volume in this slice does not justify a new HTTP client dependency, and
copying an already-reviewed pattern keeps the two Ollama clients in this
codebase consistent.

This module knows nothing about plans, schemas, or prompts -- it is the one
place `app/model_router` actually talks to Ollama's `/api/generate` for a
structured-output call (`format=json`, `temperature=0` per
docs/BUILD_LOG.md's measurement of 3/3 first-attempt schema conformance).
`app.orchestrator.plan` is what builds the prompt and validates the result.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from app.model_router import settings


class ReasoningModelError(Exception):
    """The reasoning backend could not be reached, or returned something
    unusable. Mirrors `app.rag.embeddings.EmbeddingError`'s role -- fail
    closed, never fabricate or return a partial response."""


def generate_json(
    *,
    model: str,
    prompt: str,
    temperature: Optional[float] = None,
    timeout_seconds: Optional[float] = None,
    ollama_url: Optional[str] = None,
) -> str:
    """One structured-output call to Ollama's `/api/generate`.

    Returns the raw text of the model's `response` field -- this module does
    not parse it as JSON itself; `format: "json"` only constrains Ollama's
    *sampling*, it does not guarantee the text is schema-conformant, which is
    exactly why section 5.2 still validates the result and allows one repair
    prompt. Raises `ReasoningModelError` on anything else going wrong.
    """
    if not prompt or not prompt.strip():
        raise ReasoningModelError("cannot send an empty prompt to the reasoning model")

    url = ollama_url or settings.OLLAMA_URL
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": (
                    settings.REASONING_TEMPERATURE if temperature is None else temperature
                )
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    effective_timeout = (
        settings.REASONING_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    try:
        with urllib.request.urlopen(request, timeout=effective_timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise ReasoningModelError(
            f"reasoning backend unreachable at {url} (model {model!r}): {exc}"
        ) from exc

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReasoningModelError(
            f"reasoning backend returned an invalid JSON envelope: {exc}"
        ) from exc

    text = body.get("response") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ReasoningModelError(
            f"reasoning backend returned no usable text for model {model!r}: {body!r}"
        )
    return text
