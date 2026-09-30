"""`GET /documents` and `GET /documents/{document_id}` -- browse the corpus.

Why this is safe to add: it does not widen access, it *exposes the existing
access rule to a human*. Both routes reuse `app.rag.search._passes` -- the
very function `rag.search` uses -- so a document a user may not retrieve
through an agent is equally invisible to that user in the browser. There is no
second, weaker code path.

The ACL check is against the **caller's own department and clearance**, read
from the session (section 6.4) and the `User` row, never from anything the
client sends.

Read-only. Nothing here writes, ingests, or re-classifies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.db.engine import SessionLocal
from app.db.models import User
from app.identity.dependencies import current_identity
from app.identity.tokens import SessionIdentity
from app.rag.search import Requester, _passes
from app.rag.store import get_store

router = APIRouter(tags=["documents"])


def _error(code_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code_status, detail={"error": {"code": code, "message": message}}
    )


def _requester(identity: SessionIdentity) -> Requester:
    """Build the filter subject from the *session*, never from the request."""
    with SessionLocal() as session:
        user = session.get(User, identity.user_id)
        if user is None:
            raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_USER", "session user not found")
        return Requester(
            task_id="-",
            agent_id="-",
            classification_max=user.clearance,
            department=user.department,
        )


def _documents() -> dict[str, Any]:
    """Collapse the chunk list to one row per document."""
    docs: dict[str, Any] = {}
    for chunk in get_store().chunks:
        d = docs.setdefault(
            chunk.document_id,
            {
                "document_id": chunk.document_id,
                "document_version": chunk.document_version,
                "path": chunk.document_path,
                "department": chunk.department,
                "classification": chunk.classification,
                "acl": list(chunk.acl),
                "chunks": 0,
                "_chunk": chunk,
            },
        )
        d["chunks"] += 1
    return docs


@router.get("/documents")
def list_documents(identity: SessionIdentity = Depends(current_identity)) -> dict[str, Any]:
    """Every ingested document, each marked with whether *this* caller may read
    it. Denied documents are listed by id and marking only -- never with their
    text -- so the console can show that something exists and is withheld,
    which is the point of the denial demo."""
    requester = _requester(identity)
    out = []
    for doc in _documents().values():
        allowed, reason = _passes(doc.pop("_chunk"), requester)
        doc["readable"] = allowed
        doc["reason"] = None if allowed else reason
        out.append(doc)
    out.sort(key=lambda d: (not d["readable"], d["document_id"]))
    return {
        "requester": {
            "department": requester.department,
            "clearance": requester.classification_max,
        },
        "documents": out,
    }


@router.get("/documents/{document_id}")
def get_document(
    document_id: str, identity: SessionIdentity = Depends(current_identity)
) -> dict[str, Any]:
    requester = _requester(identity)
    docs = _documents()
    doc = docs.get(document_id)
    if doc is None:
        raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_DOCUMENT", f"no document {document_id!r}")

    allowed, reason = _passes(doc.pop("_chunk"), requester)
    if not allowed:
        # Same rule, same wording as the Data Plane's own filter.
        raise _error(status.HTTP_403_FORBIDDEN, "ACCESS_DENIED", reason)

    content = None
    read_error = None
    try:
        content = Path(doc["path"]).read_text(encoding="utf-8")
    except OSError as exc:
        read_error = str(exc)

    doc["content"] = content
    doc["read_error"] = read_error
    doc["readable"] = True
    return doc
