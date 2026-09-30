"""Embedding + similarity, against the locally-serving Ollama instance.

Verified 2026-09-07 (docs/BUILD_LOG.md, Environment section): `nomic-embed-text`
serves at `http://localhost:11434`, returns 768-dim vectors, no truncation on
this corpus. No new model is pulled here or anywhere in this package.

Deliberately stdlib-only (`urllib.request`, `json`, `math`) -- no `numpy`, no
HTTP client dependency beyond what already ships with Python. The corpus is a
few hundred chunks at most; a numpy-free cosine loop is the MVP choice (see
the step-6 handoff notes and design doc section 10's non-goals).
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Sequence

from app.rag import settings


class EmbeddingError(Exception):
    """The embedding backend could not be reached, or returned something
    unusable. Both ingestion and search fail closed on this -- a chunk that
    cannot be embedded is never silently stored with an all-zero vector, and
    a query that cannot be embedded never falls back to returning everything
    unranked."""


def embed_text(text: str) -> list[float]:
    """Return the embedding vector for `text` via Ollama's `/api/embeddings`.

    Raises `EmbeddingError` on any failure -- unreachable server, malformed
    response, or an empty embedding list. Never returns a partial or
    fabricated vector.
    """
    if not text or not text.strip():
        raise EmbeddingError("cannot embed empty text")

    payload = json.dumps({"model": settings.EMBEDDING_MODEL, "prompt": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.EMBEDDING_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise EmbeddingError(
            f"embedding backend unreachable at {settings.OLLAMA_URL} "
            f"(model {settings.EMBEDDING_MODEL!r}): {exc}"
        ) from exc

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"embedding backend returned invalid JSON: {exc}") from exc

    embedding = body.get("embedding") if isinstance(body, dict) else None
    if not isinstance(embedding, list) or not embedding:
        raise EmbeddingError(
            f"embedding backend returned no usable vector for model "
            f"{settings.EMBEDDING_MODEL!r}: {body!r}"
        )
    return [float(x) for x in embedding]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain-Python cosine similarity. `0.0` for a zero vector on either side
    rather than raising -- a defensive floor, since that vector would never
    legitimately come back from `embed_text`."""
    if len(a) != len(b):
        raise EmbeddingError(f"embedding dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
