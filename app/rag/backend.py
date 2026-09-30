"""The `rag.search` Tool Gateway backend (design doc section 6.6 Step C,
section 6.9's contract).

Wired in exactly like every other backend (`app/tool_gateway/backends/echo.py`'s
docstring is the reference):

    from app.policy import Tool
    from app.tool_gateway import register_backend
    from app.rag.backend import rag_search_backend

    register_backend(Tool.RAG_SEARCH, rag_search_backend)

By the time this function is called, the Tool Gateway has already verified
the capability (Step A) and the Policy Engine has already ALLOWed the
*operation* (Step B) -- `app.rag.search` (this package's own module) is what
then decides which specific documents may appear in the result, which is the
one thing Step B could not have decided: it only had `task_resource(task_id)`
to look at, not the concrete documents a free-text query might match.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.observability import EventType, append_event
from app.rag.search import Requester, search
from app.rag.store import get_store
from app.tool_gateway.registry import ToolRequest


def rag_search_backend(request: ToolRequest) -> Mapping[str, Any]:
    """Search the one vector store (section 6.9), filtered inside the Data
    Plane before anything is returned, then emit `EVIDENCE_RETRIEVED`.

    Returns `{"results": [...]}` -- the section 6.9 result body. Never
    returns a document the requester was not entitled to see; a filtered
    document is recorded in the emitted event by id/classification/acl only,
    never by its text.
    """
    query = request.arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("rag search requires a non-empty 'query' string argument")

    requester = Requester.from_mapping(request.requester())
    outcome = search(query, requester, store=get_store())

    append_event(
        request.capability.task_id,
        request.capability.agent_id,
        EventType.EVIDENCE_RETRIEVED,
        {
            "query": query,
            "requester": {
                "task_id": requester.task_id,
                "agent_id": requester.agent_id,
                "classification_max": requester.classification_max,
                "department": requester.department,
            },
            "returned_count": len(outcome.results),
            "evidence_ids": [row["evidence_id"] for row in outcome.results],
            "total_candidate_chunks": outcome.total_candidate_chunks,
            "allowed_candidate_chunks": outcome.allowed_candidate_chunks,
            "filtered_chunk_count": outcome.filtered_chunk_count,
            "filtered_document_count": outcome.filtered_document_count,
            "filtered_documents": [d.to_dict() for d in outcome.filtered_documents],
            "execution_id": request.execution_id,
        },
    )

    return {"results": outcome.results}
