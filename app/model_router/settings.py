"""Model Router settings (design doc section 6.3).

Deliberately its own module rather than an addition to `app/config.py` --
matching the pattern `app/rag/settings.py` and `app/execution/settings.py`
already established: everything environment-driven, read from `os.environ`
directly, never imported by `app/config.py` (no cycle).

`OLLAMA_URL` intentionally reads the *same* environment variable
`app/rag/settings.py` reads (`CITADEL_OLLAMA_URL`) -- both packages are
independent HTTP clients of the one local Ollama instance (design doc
section 8's "a locally-served open-weight model, e.g. via Ollama, for
reasoning + embeddings"), so one operator-facing variable configures both
without the two packages importing each other.
"""

from __future__ import annotations

import os

OLLAMA_URL: str = os.environ.get("CITADEL_OLLAMA_URL", "http://localhost:11434")

#: The planning call is a single, larger structured-output generation --
#: BUILD_LOG.md measured 7-13s per call against `hermes3`. Generous headroom
#: over that, plus the one repair retry, still needs to fit comfortably
#: inside a synchronous HTTP request for the demo.
REASONING_TIMEOUT_SECONDS: float = float(
    os.environ.get("CITADEL_MODEL_ROUTER_TIMEOUT_SECONDS", "90")
)

#: section 5.2's structured-output call must be deterministic enough that
#: schema validation is meaningful to test -- temperature 0, matching what
#: BUILD_LOG.md verified achieves 3/3 first-attempt schema conformance.
REASONING_TEMPERATURE: float = float(
    os.environ.get("CITADEL_MODEL_ROUTER_TEMPERATURE", "0")
)
