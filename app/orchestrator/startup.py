"""Process-startup wiring: register the three tool backends, ingest the demo
corpus (design doc section 6.6 Step C, section 6.9).

Called once by `app.main`'s FastAPI startup hook for a real deployment, and
directly by tests/demos that want the real backends without going through
FastAPI's lifecycle -- the same pattern `tests/demos/step5_execution.py` and
`tests/demos/step6_rag.py` already use, just gathered into one function so
`/task` actually works end to end when the app is run for real.

`python.execute` still needs a reachable Execution Service
(`CITADEL_EXECUTION_SERVICE_URL`, section 2) -- this module does not start
one; that is a separate OS process by design (section 2, step 5), started by
`docker-compose.yml` or by hand.
"""

from __future__ import annotations

from pathlib import Path

from app.artifact.backend import register as register_report_backend
from app.execution import register as register_execution_backend
from app.policy import Tool
from app.rag.backend import rag_search_backend
from app.rag.ingest import ingest_paths
from app.rag.store import VectorStore, set_store
from app.tool_gateway import register_backend

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

#: An explicit, known-good list -- not `ingest_directory(DATA_ROOT, ...)`.
#: `data/maintenance/pump_p102_notes.txt` ships without a sidecar by design
#: (design doc section 6.9's mandatory-sidecar rule, `data/README.md`), so a
#: whole-directory ingest raises `MissingSidecarError` (see
#: docs/BUILD_LOG.md's Step 6 section, decision 3). Naming the documents
#: this slice's scenario actually needs is both correct and simpler.
_KNOWN_GOOD_DOCUMENTS = [
    DATA_ROOT / "maintenance" / "pump_p101_history.txt",
    DATA_ROOT / "maintenance" / "pump_p101_spec_sheet.txt",
    DATA_ROOT / "finance" / "q3_finance_report.txt",
]


def register_backends() -> None:
    register_backend(Tool.RAG_SEARCH, rag_search_backend)
    register_execution_backend()
    register_report_backend()


def ingest_demo_corpus(*, store: VectorStore | None = None) -> VectorStore:
    store = store if store is not None else VectorStore()
    ingest_paths(_KNOWN_GOOD_DOCUMENTS, store=store)
    set_store(store)
    return store


def configure() -> None:
    """The one call `app.main`'s startup hook makes."""
    register_backends()
    ingest_demo_corpus()
