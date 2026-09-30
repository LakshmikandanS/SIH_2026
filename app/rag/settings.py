"""Data Plane / RAG settings (design doc section 6.9).

Deliberately separate from `app/config.py` -- that module belongs to
`foundation-schema`/`security-control-plane` and this step does not edit it
(see the step-6 handoff notes). The style is copied: everything
environment-driven lives here, read from `os.environ` directly, so nothing
else in `app/rag` needs to know an environment variable exists.

Nothing in this module imports from the rest of `app`, matching
`app/config.py`'s own rule, so it can be read by any component without
creating a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root (this file is at <root>/app/rag/settings.py).
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"
VAR_DIR = ROOT_DIR / "var"

# --- Ingestion ------------------------------------------------------------
#: The mandatory sidecar suffix (design doc section 6.9). A source document at
#: `data/maintenance/pump_p101_history.pdf` must ship
#: `data/maintenance/pump_p101_history.pdf.meta.json` beside it, or ingestion
#: rejects it outright -- no default classification, no inferred ACL.
SIDECAR_SUFFIX: str = ".meta.json"

#: Filenames ingestion never treats as a source document, wherever they sit
#: under `data/`. `README.md` documents the corpus; it is not part of it.
EXCLUDED_FILENAMES: frozenset[str] = frozenset({"README.md"})

#: Paragraphs shorter than this are merged into the next one before being
#: embedded, so a lone section heading ("MAINTENANCE LIMITS") never becomes
#: its own near-content-free chunk. Purely a chunk-quality knob.
MIN_CHUNK_CHARS: int = int(os.environ.get("CITADEL_RAG_MIN_CHUNK_CHARS", "40"))

# --- Embedding (Ollama) -----------------------------------------------------
#: Verified 2026-09-07 serving locally: `nomic-embed-text`, 768-dim, no
#: truncation on this corpus (see docs/BUILD_LOG.md, Environment section).
OLLAMA_URL: str = os.environ.get("CITADEL_OLLAMA_URL", "http://localhost:11434")
EMBEDDING_MODEL: str = os.environ.get("CITADEL_RAG_EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_TIMEOUT_SECONDS: float = float(
    os.environ.get("CITADEL_RAG_EMBEDDING_TIMEOUT_SECONDS", "30")
)

# --- Retrieval ---------------------------------------------------------------
#: How many chunks `rag.search` returns at most, after ACL/classification
#: filtering and ranking.
TOP_K: int = int(os.environ.get("CITADEL_RAG_TOP_K", "5"))

#: A relevance floor on cosine similarity, empirically set against this demo
#: corpus and `nomic-embed-text` (see docs/notes/step6.md). This is NOT a
#: security control -- the ACL/classification filter above it is
#: unconditional and runs regardless of this value. It exists only so an
#: irrelevant-but-permitted document does not pad out the result list, and as
#: a side effect it is what makes the mandatory denial-path demo (design doc
#: section 1.2) come back with zero rows rather than a handful of weakly
#: related, technically-permitted ones.
MIN_SCORE: float = float(os.environ.get("CITADEL_RAG_MIN_SCORE", "0.5"))

# --- Storage ------------------------------------------------------------------
#: Optional JSON persistence path for `VectorStore.save`/`.load`. Not read or
#: written automatically by anything in this package -- ingestion populates an
#: in-memory `VectorStore` and the caller (a demo, a test, eventually the
#: Orchestrator's startup routine) decides whether to persist it. Provided so
#: "swap in a file path to survive a restart" is a one-line change rather than
#: a new module (see docs/notes/step6.md's Phase-2 seams).
DEFAULT_STORE_PATH: Path = VAR_DIR / "rag_store.json"
