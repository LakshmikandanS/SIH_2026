"""docs.search, docs.read, docs.diff and web.search: the corpus, through the same
permission predicate the knowledge package applies in SQL, behind a receipt checked at
the data boundary.

web.search is not a web client. This deployment has no route out (invariant 10); the
tool searches the offline reference library -- PUBLIC vendor literature and technology
digests a person imported and reviewed -- and nothing else (docs/adr/0010). Which
folder that is comes from the tool's registry options, never from code.

Search results go to the model as short, numbered evidence (E1, E2...) it can cite.
What the caller may not see is not silently absent: the count of withheld documents
and the reason for each (classification or department) travel with the result -- never
their titles or text, which is the point of withholding them.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from citadel_contracts.domain import Resource
from citadel_knowledge import SearchScope, diff_versions, document_facts, document_versions, read_page, search

from citadel_tools.context import Invocation, ResourceNotFound, ToolContext, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin, digest_of

#: How much of each passage a model sees -- enough to quote a number and its context,
#: small enough that eight passages fit a small model's window with room to reason.
PASSAGE_CHARS = 700
PAGE_CHARS = 5000
_TERM = re.compile(r"[a-z0-9]+")
_BREAK = re.compile(r"\n|(?<=[.;:])\s")


def passage_window(text: str, query: str, limit: int = PASSAGE_CHARS) -> str:
    """The part of a long passage the query is about. A passage longer than the model
    may see is shown from the line where the query's words are densest -- not blindly
    from its start, which would hide a fact in its last lines -- and says it is cut."""
    if len(text) <= limit:
        return text
    lowered = text.lower()
    # A word that occurs once in the passage says more about where to look than one that
    # occurs on every line: each term counts for the share of its occurrences a window holds.
    counts = {t: lowered.count(t) for t in _TERM.findall(query.lower()) if len(t) > 2}
    counts = {t: n for t, n in counts.items() if n}
    best, best_score = 0, -1.0
    for start in [0, *(m.end() for m in _BREAK.finditer(text))]:
        if start >= len(text):
            continue
        window = lowered[start:start + limit]
        score = sum(window.count(term) / n for term, n in counts.items())
        if score > best_score + 1e-9:
            best, best_score = start, score
    snippet = text[best:best + limit].strip()
    return ("... " if best > 0 else "") + snippet + (" ..." if best + limit < len(text) else "")


def scope_for(ctx: ToolContext) -> SearchScope:
    return SearchScope(ctx.department, ctx.actor.classification_max, ctx.user.user_id)


def _search_resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    key = digest_of(f"{args['query']}|{args.get('top_k')}")
    return Resource.build(f"corpus-search:{ctx.task_id}:{key}", "corpus", ctx.actor.classification_max, (ctx.department,))


def _options(ctx: ToolContext, name: str) -> dict[str, Any]:
    try:
        return dict(ctx.registry.tool(name).options or {})
    except KeyError:
        return {}


def _search(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    return _run_search(ctx, args, invocation, tool="docs.search")


def _web_search(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    output = _run_search(ctx, args, invocation, tool="web.search")
    output.data["source"] = "offline reference library (no internet access from this deployment)"
    output.summary = "reference library: " + output.summary
    return output


def _run_search(ctx: ToolContext, args: dict[str, Any], invocation: Invocation, *, tool: str) -> ToolOutput:
    ctx.boundary.verify(invocation, _search_resource(ctx, args))
    options = _options(ctx, tool)
    top_k = int(args.get("top_k") or (6 if tool == "web.search" else 8))
    within = str(options["within_folder"]) if options.get("within_folder") else None
    outside = str(options["outside_folder"]) if options.get("outside_folder") else None
    result = search(ctx.db, ctx.gateway, scope_for(ctx), str(args["query"]), top_k=top_k, task_id=ctx.task_id,
                    within_folder=within, outside_folder=outside)
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
            "text": passage_window(h.text, str(args["query"])),
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
    issues = document_versions(ctx.db, scope_for(ctx), document_id)
    if len(issues) > 1:
        data["versions"] = [
            {"version": v["version"], "effective": v.get("effective"), "change_note": v.get("change_note"),
             "current": v.get("current")}
            for v in issues
        ]
        data["about_versions"] = "Earlier issues are readable with version=N; docs.diff shows what changed."
    return ToolOutput(
        data=data,
        summary=f"read {document['title']} v{page['version']} page {page_number}/{page_count} ({len(page['blocks'])} blocks)",
        evidence=evidence,
    )


def _diff(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    ctx.boundary.verify(invocation, _document_resource(ctx, args))
    document_id = str(args["document_id"]).strip()
    issues = document_versions(ctx.db, scope_for(ctx), document_id)
    if not issues:
        raise ResourceNotFound(f"document {document_id} is not visible to this task")
    current = max(int(v["version"]) for v in issues)
    to_version = int(args.get("to_version") or current)
    from_version = int(args.get("from_version") or max(1, to_version - 1))
    if from_version == to_version:
        raise ToolFailure("from_version and to_version are the same issue; there is nothing to compare")
    known = {int(v["version"]) for v in issues}
    for version in (from_version, to_version):
        if version not in known:
            raise ResourceNotFound(f"document {document_id} has no version {version} (it has {sorted(known)})")
    result = diff_versions(ctx.db, scope_for(ctx), document_id, from_version=from_version, to_version=to_version,
                           task_id=ctx.task_id)
    if result is None:
        raise ResourceNotFound(f"document {document_id} is not visible to this task")
    by_version = {int(v["version"]): v for v in issues}
    changes = result["changes"]
    evidence = sorted({e for c in changes for e in [*c["before_evidence"], *c["after_evidence"]] if e},
                      key=lambda e: int(e[1:]))
    data = {
        "document": result["document"]["title"],
        "document_id": document_id,
        "from": {"version": from_version, "effective": by_version[from_version].get("effective"),
                 "change_note": by_version[from_version].get("change_note")},
        "to": {"version": to_version, "effective": by_version[to_version].get("effective"),
               "change_note": by_version[to_version].get("change_note")},
        "changes": changes,
        "unchanged_sentences": result.get("unchanged_sentences"),
        "how_to_cite": "Cite the old wording by its before_evidence id and the new by its after_evidence id.",
    }
    return ToolOutput(
        data=data,
        summary=f"{len(changes)} change(s) between v{from_version} and v{to_version} of {result['document']['title']}",
        evidence=evidence,
    )


PLUGINS = {
    "docs.search": ToolPlugin("docs.search", _search_resource, _search),
    "docs.read": ToolPlugin("docs.read", _document_resource, _read),
    "docs.diff": ToolPlugin("docs.diff", _document_resource, _diff),
    "web.search": ToolPlugin("web.search", _search_resource, _web_search),
}

__all__ = ["PLUGINS", "scope_for"]
