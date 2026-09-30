"""The one vector store (design doc section 6.9).

    "'Organizational Knowledge' and the RAG vector store are declared
     identical for this MVP -- there is exactly one vector store; don't build
     a separate 'Organizational Knowledge' system."

No Qdrant, Chroma or FAISS (design doc section 10's non-goals) -- a plain
in-memory list of chunks, each carrying its own embedding, searched with a
numpy-free cosine loop (`app.rag.embeddings.cosine_similarity`). Optional JSON
persistence (`save`/`load`) is the Phase-2 seam for surviving a process
restart; nothing in this package calls either automatically.

The module-level default instance follows the same pattern as
`app.policy.tool_disabled` (`get_registry`/`set_registry`) and
`app.tool_gateway.registry` (`register_backend`/`clear_backends`): one
process-global object, swappable so tests never share state with each other
or with the demo.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class Chunk:
    """One embedded chunk of one ingested document.

    Not a section 3 domain object -- `Evidence` (app/rag/evidence.py) is the
    thing that crosses the Tool Gateway boundary. A `Chunk` is this module's
    own storage row and never leaves the Data Plane directly; `Evidence.
    from_chunk` is the only bridge, and it is built only for chunks that
    already passed the ACL/classification filter (app/rag/search.py).
    """

    document_id: str
    document_version: str
    document_path: str
    #: The sidecar's own `department` field -- informational/audit metadata
    #: about who authored the document. Filtering never reads this; it reads
    #: `acl` against the *requester's* department (design doc section 6.9).
    department: str
    classification: str
    acl: tuple[str, ...]
    page: int
    text: str
    embedding: list[float]


class VectorStore:
    """An in-memory, thread-safe collection of `Chunk`s.

    Every write (`add`, `clear`, `load`) and every read of the live list
    (`chunks`) takes the same lock, so a search can never observe a partially
    -written ingestion batch.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[Chunk] = []

    def add(self, chunk: Chunk) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()

    @property
    def chunks(self) -> list[Chunk]:
        """A snapshot list -- mutating it never mutates the store."""
        with self._lock:
            return list(self._chunks)

    def __len__(self) -> int:
        with self._lock:
            return len(self._chunks)

    # -- optional persistence (Phase-2 seam; nothing auto-calls these) -----

    def save(self, path: Union[str, Path]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "document_id": c.document_id,
                "document_version": c.document_version,
                "document_path": c.document_path,
                "department": c.department,
                "classification": c.classification,
                "acl": list(c.acl),
                "page": c.page,
                "text": c.text,
                "embedding": c.embedding,
            }
            for c in self.chunks
        ]
        path.write_text(json.dumps(rows), encoding="utf-8")

    def load(self, path: Union[str, Path]) -> None:
        """Replace this store's contents with what was saved at `path`."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        loaded = [
            Chunk(
                document_id=row["document_id"],
                document_version=row["document_version"],
                document_path=row["document_path"],
                department=row["department"],
                classification=row["classification"],
                acl=tuple(row["acl"]),
                page=row["page"],
                text=row["text"],
                embedding=list(row["embedding"]),
            )
            for row in raw
        ]
        with self._lock:
            self._chunks = loaded


# --- module-level default instance, swappable like the other registries ----
_default_store = VectorStore()
_swap_lock = threading.Lock()


def get_store() -> VectorStore:
    """The process-wide default store the `rag.search` backend reads from
    when no store is given explicitly."""
    with _swap_lock:
        return _default_store


def set_store(store: VectorStore) -> VectorStore:
    """Swap the default store. Returns the previous one so it can be
    restored -- the pattern every test in this suite uses to avoid leaking
    corpus state between tests."""
    global _default_store
    with _swap_lock:
        previous = _default_store
        _default_store = store
        return previous


def reset_store() -> VectorStore:
    """Test-suite convenience: install and return a fresh, empty store."""
    return set_store(VectorStore())
