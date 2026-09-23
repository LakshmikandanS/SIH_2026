"""docs.search and docs.read: the corpus, through the same permission predicate the
knowledge package applies in SQL, behind a receipt checked at the data boundary.

Search results go to the model as short, numbered evidence (E1, E2...) it can cite.
What the caller may not see is not silently absent: the count of withheld documents
and the reason for each (classification or department) travel with the result -- never
their titles or text, which is the point of withholding them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from citadel_contracts.domain import Resource
from citadel_knowledge import SearchScope, document_facts, read_page, search

from citadel_tools.context import Invocation, ResourceNotFound, ToolContext, ToolOutput
from citadel_tools.plugins import ToolPlugin, digest_of

#: How much of each passage a model sees -- enough to quote a number and its context,
#: small enough that eight passages fit a small model's window with room to reason.
PASSAGE_CHARS = 700
PAGE_CHARS = 5000


def scope_for(ctx: ToolContext) -> SearchScope:
    return SearchScope(ctx.department, ctx.actor.classification_max, ctx.user.user_id)


def _search_resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    key = digest_of(f"{args['query']}|{args.get('top_k')}")
    return Resource.build(f"corpus-search:{ctx.task_id}:{key}", "corpus", ctx.actor.classification_max, (ctx.department,))


def _search(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    ctx.boundary.verify(invocation, _search_resource(ctx, args))
    top_k = int(args.get("top_k") or 8)
    result = search(ctx.db, ctx.gateway, scope_for(ctx), str(args["query"]), top_k=top_k, task_id=ctx.task_id)
    for denied in result.denied:
        if ctx.audit is not None:
            ctx.audit.record(
                "retrieval.denied_doc",
                actor_id=ctx.user.user_id,
                payload={"task_id": ctx.task_id, **denied.to_dict()},
            )
    reasons = Counter(d.reason for d in result.denied)
    passages = [
        {
            "evidence_id": h.evidence_id,
            "document": h.title,
            "document_id": h.document_id,
            "page": h.page,
            "classification": h.classification.upper(),
            "text": h.text[:PASSAGE_CHARS],
        }
        for h in result.hits
    ]
    documents = len({h.document_id for h in result.hits})
    withheld = ", ".join(f"{n} by {reason}" for reason, n in reasons.items())
    summary = f"{len(result.hits)} passage(s) from {documents} document(s)"
    if result.denied:
        summary += f"; {len(result.denied)} document(s) withheld ({withheld})"
    data: dict[str, Any] = {
        "passages": passages,
        "withheld_documents": {"count": len(result.denied), "reasons": dict(reasons)},
        "how_to_cite": "Cite a passage by its evidence id in square brackets, e.g. [E1].",
    }
    if result.stats.get("degraded"):
        data["degraded"] = result.stats["degraded"]
    return ToolOutput(
        data=data,
        summary=summary,
        evidence=[h.evidence_id for h in result.hits if h.evidence_id],
        detail={"search": result.to_dict()},
    )


def _document_resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    document_id = str(args["document_id"]).strip()
    facts = document_facts(ctx.db, document_id)
    if facts is None:
        raise ResourceNotFound(f"no document with id {document_id!r}; use a document_id from docs.search results")
    return Resource.build(document_id, "document", str(facts["classification"]).upper(), tuple(facts["acl"]))


def _read(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    ctx.boundary.verify(invocation, _document_resource(ctx, args))
    document_id = str(args["document_id"]).strip()
    page_number = int(args.get("page") or 1)
    page = read_page(
        ctx.db, scope_for(ctx), document_id, page=page_number, version=args.get("version"), task_id=ctx.task_id
    )
    if page is None or page.get("page") is None:
        raise ResourceNotFound(f"document {document_id} has no page {page_number} visible to this task")
    document = page["document"]
    blocks = []
    used = 0
    for block in page["blocks"]:
        text = str(block["text"])
        if used + len(text) > PAGE_CHARS:
            blocks.append({"kind": "note", "text": "[page text truncated]"})
            break
        used += len(text)
        blocks.append({"kind": block["kind"], "source": block["source"], "text": text})
    page_count = int(document.get("page_count") or 1)
    evidence = [e["evidence_id"] for e in page["evidence"] if e.get("evidence_id")]
    data = {
        "document": document["title"],
        "document_id": document_id,
        "version": page["version"],
        "page": page_number,
        "page_count": page_count,
        "text_source": page["page"].get("text_source"),
        "blocks": blocks,
        "evidence": [{"evidence_id": e["evidence_id"], "text": str(e["text"])[:240]} for e in page["evidence"]],
        "how_to_cite": "Cite this page's passages by their evidence ids, e.g. [" + (evidence[0] if evidence else "E1") + "].",
    }
    if page_number < page_count:
        data["next_page"] = page_number + 1
    return ToolOutput(
        data=data,
        summary=f"read {document['title']} page {page_number}/{page_count} ({len(page['blocks'])} blocks)",
        evidence=evidence,
    )


PLUGINS = {
    "docs.search": ToolPlugin("docs.search", _search_resource, _search),
    "docs.read": ToolPlugin("docs.read", _document_resource, _read),
}

__all__ = ["PLUGINS", "scope_for"]
