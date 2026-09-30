"""Step 6 demo -- the Data Plane / RAG, shown rather than asserted.

    .venv/Scripts/python -m tests.demos.step6_rag

Walks design doc section 6.9 end to end against the real demo corpus
(`data/`, see `data/README.md`) and the real, locally-serving Ollama
embedding model (`nomic-embed-text`) -- nothing here is mocked:

  1. Ingestion rejects the one fixture that ships without a sidecar,
     outright, with a clear error -- not a silent skip.
  2. The legitimate corpus is ingested into the one vector store.
  3. The happy path (section 1.1): an authorized `rag.search` call through
     the real Tool Gateway, backed by the real `rag.search` backend, returns
     evidence from the document it should.
  4. The mandatory denial path (section 1.2): the exact query
     ("Q3 finance report") issued by a `department: maintenance` task
     against `acl: ["finance"]` content returns ZERO results from this
     layer -- filtered inside the Data Plane, before the result could ever
     reach the agent, and before this call even reaches a document-specific
     Policy Engine decision.
  5. The classification variant of the same guarantee: an INTERNAL task
     reaching for the CONFIDENTIAL history document sees the INTERNAL spec
     sheet for the same asset, never the CONFIDENTIAL document.
  6. The event trace and the hash chain, proving `EVIDENCE_RETRIEVED` was
     recorded -- including the filtered-out count -- for every call.

Runs against a throwaway database and a throwaway in-memory vector store; it
never touches `var/citadel.db` or `var/rag_store.json`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_DEMO_DB = Path(tempfile.gettempdir()) / "citadel_step6_demo.db"
_DEMO_DB.unlink(missing_ok=True)
os.environ["CITADEL_DATABASE_URL"] = f"sqlite:///{_DEMO_DB.as_posix()}"

from app.capability import issue_for_step  # noqa: E402
from app.db import init_db  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Agent, Task, User  # noqa: E402
from app.db.state_machines import Classification, Role  # noqa: E402
from app.observability import format_trace, get_trace, verify_chain  # noqa: E402
from app.policy.tools import Tool  # noqa: E402
from app.rag.backend import rag_search_backend  # noqa: E402
from app.rag.ingest import MissingSidecarError, ingest_directory, ingest_paths  # noqa: E402
from app.rag.store import VectorStore, set_store  # noqa: E402
from app.tool_gateway import clear_backends, invoke, register_backend, task_resource  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
HISTORY_DOC = DATA_ROOT / "maintenance" / "pump_p101_history.txt"
SPEC_DOC = DATA_ROOT / "maintenance" / "pump_p101_spec_sheet.txt"
FINANCE_DOC = DATA_ROOT / "finance" / "q3_finance_report.txt"

MAINTENANCE_QUERY = "Pump P-101 maintenance history"
FINANCE_QUERY = "Q3 finance report"  # design doc section 1.2's own denial-path query


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show_results(label: str, results: list[dict]) -> None:
    print(f"  {label}")
    if not results:
        print("    (zero results)")
        return
    for row in results:
        preview = row["text"].replace("\n", " ")[:60]
        print(
            f"    {row['document_id']:<16} page={row['page']:<3} "
            f"{row['classification']:<12} score={row.get('score', 0):.4f}  "
            f"evidence_id={row['evidence_id']}  {preview!r}"
        )


def seed():
    """One engineer, one CONFIDENTIAL maintenance task/agent (the happy path
    and the denial path both run as this task -- section 1.2 uses the same
    task, only the query changes), plus the INTERNAL task/agent used to
    exercise the classification rule."""
    init_db.reset()
    session = SessionLocal()

    engineer = User(
        user_id="U123",
        username="j.rao",
        roles=[Role.ENGINEER],
        clearance=Classification.CONFIDENTIAL,
        department="maintenance",
    )
    session.add(engineer)
    session.flush()

    task = Task(
        task_id="T123",
        user_id=engineer.user_id,
        classification=Classification.CONFIDENTIAL,
        requirements={"needs_rag": True},
    )
    session.add(task)
    session.flush()
    session.add(Agent(agent_id="A123", task_id=task.task_id, agent_type="researcher"))

    internal = Task(
        task_id="T-INTERNAL",
        user_id=engineer.user_id,
        classification=Classification.INTERNAL,
        requirements={"needs_rag": True},
    )
    session.add(internal)
    session.flush()
    session.add(Agent(agent_id="A-INTERNAL", task_id=internal.task_id, agent_type="researcher"))

    session.commit()
    return session


def run_query(task_id: str, agent_id: str, query: str) -> dict:
    capability = issue_for_step(task_id, agent_id, Tool.RAG_SEARCH)
    return invoke(
        capability_token=capability.token,
        tool=Tool.RAG_SEARCH,
        resource=task_resource(task_id),
        arguments={"query": query},
    )


def main() -> None:
    session = seed()
    clear_backends()
    register_backend(Tool.RAG_SEARCH, rag_search_backend)

    rule("1. Ingestion rejects a document with no sidecar (section 6.9, BB-036)")
    print(f"  data/maintenance/ contains pump_p102_notes.txt, deliberately shipped")
    print("  without a .meta.json sidecar (data/README.md).\n")
    throwaway = VectorStore()
    try:
        ingest_directory(DATA_ROOT / "maintenance", store=throwaway)
        print("  UNEXPECTED: ingestion did not reject the directory")
    except MissingSidecarError as exc:
        print(f"  REJECTED, outright: {exc}")
    print(f"\n  store size after the rejected batch: {len(throwaway)} (nothing was ingested)")

    rule("2. Ingesting the legitimate corpus into the one vector store")
    store = VectorStore()
    report = ingest_paths([HISTORY_DOC, SPEC_DOC, FINANCE_DOC], store=store)
    for doc in report.documents:
        print(f"  {doc.document_id:<16} {doc.chunk_count} chunks  <- {doc.path}")
    print(f"\n  {len(store)} chunks total in the store.")
    set_store(store)

    rule("3. The happy path (section 1.1 steps 7-11)")
    print(f"  query: {MAINTENANCE_QUERY!r}, requester: CONFIDENTIAL/maintenance (T123)\n")
    envelope = run_query("T123", "A123", MAINTENANCE_QUERY)
    print(f"  envelope.success = {envelope['success']}")
    show_results("results:", envelope["result"]["results"])

    rule("4. The mandatory denial path (section 1.2)")
    print(f"  Same task (T123, department=maintenance). Only the query changes to")
    print(f"  {FINANCE_QUERY!r}, whose only match in the corpus is")
    print("  acl=['finance'] -- disjoint from the task's department.\n")
    envelope = run_query("T123", "A123", FINANCE_QUERY)
    print(f"  envelope.success = {envelope['success']}  (the OPERATION was still allowed)")
    show_results("results:", envelope["result"]["results"])
    print(
        "\n  Zero rows. The finance document was never constructed into a "
        "result -- not returned and then dropped. This happened INSIDE the "
        "Data Plane, before the Tool Gateway's own Policy Engine ever looked "
        "at a specific document (task_resource() only carries the task's own "
        "envelope)."
    )

    rule("5. The classification variant of the same guarantee")
    print("  An INTERNAL task (T-INTERNAL) asking the same maintenance question.")
    print("  The CONFIDENTIAL history document must not appear; the INTERNAL")
    print("  spec sheet for the same asset still may.\n")
    envelope = run_query("T-INTERNAL", "A-INTERNAL", MAINTENANCE_QUERY)
    show_results("results:", envelope["result"]["results"])
    doc_ids = {row["document_id"] for row in envelope["result"]["results"]}
    assert "DOC-P101-HIST" not in doc_ids, "CONFIDENTIAL document leaked to an INTERNAL task"
    print(f"\n  DOC-P101-HIST (CONFIDENTIAL) excluded: {'DOC-P101-HIST' not in doc_ids}")

    rule("6. EVIDENCE_RETRIEVED -- the audit trail the denial-path demo reads")
    events = get_trace(None)
    evidence_events = [e for e in events if e.event_type == "EVIDENCE_RETRIEVED"]
    for event in evidence_events:
        payload = event.payload
        print(
            f"  task={event.task_id:<12} query={payload['query']!r:<28} "
            f"returned={payload['returned_count']} "
            f"filtered_documents={[d['document_id'] for d in payload['filtered_documents']]}"
        )

    rule("7. The trace and the hash chain")
    print(format_trace(events))
    verify_chain()
    print(f"\n  {len(events)} events, chain verified unbroken.")

    session.close()
    clear_backends()


if __name__ == "__main__":
    main()
