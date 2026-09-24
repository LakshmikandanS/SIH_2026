"""Retrieval: ACL predicate -> hybrid candidates (dense + lexical) -> fusion -> rerank
-> citations, with the denial record kept (packages/knowledge/AGENTS.md).

## Filter before rank -- the cardinal rule of this package

The permission predicate is a WHERE clause in the *same* SQL statement as the vector
search and the full-text search, so a chunk the caller may not see is never read into
Python at all, let alone ranked and dropped. The predicate is built from the caller's
*verified* identity: the set of classification levels their clearance dominates --
computed here with `Classification.exceeds`, the one lattice, never string ordering --
and their department, which must appear in the chunk's ACL.

Dense search relies on `hnsw.iterative_scan = strict_order` (migration 0004): without
it a restrictive filter lets HNSW stop early and return fewer than k rows.

## The denial record

ADR-0001 §Q4: moving the filter into SQL "silently loses" the prototype's
DeniedDocument -- document id, classification, ACL and reason, never the text -- and
without it "ACL filtering works" is unfalsifiable. So one extra aggregate query asks
which *documents* would have matched but are withheld, and returns only those four
facts. It is what the multi-user demonstration puts on screen (ADR-0001 §Q7).

## Rerank

The top candidates are reranked on the CPU by query-term coverage blended with the
fused rank. A cross-encoder (the design target, packages/knowledge/AGENTS.md) needs a
model nobody has approved yet; `stats["rerank"]` says which method ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_gateway import Gateway, NoEligibleModel, ProviderError
from citadel_platform.db import Database, Json, RealArray, Vector

#: The lattice's members, bottom to top, named through the class so a renamed level
#: fails here loudly. Filtering with `Classification.exceeds` (not string order) is
#: what decides which of them a caller may see.
LATTICE = (Classification.PUBLIC, Classification.INTERNAL, Classification.CONFIDENTIAL)

_TOKEN = re.compile(r"[a-z0-9]+")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were what which "
    "with who whom does do did how why when where please show find give tell me about all any".split()
)
_RRF_K = 60
_CANDIDATES = 24

REASON_CLASSIFICATION = "classification_exceeds_actor_max"
REASON_ACL = "acl_disjoint_from_department"


def is_uuid(value: str) -> bool:
    return bool(_UUID.match(value))


@dataclass(frozen=True)
class SearchScope:
    """Who is asking, reduced to the two facts the predicate needs. Built by the
    trusted caller from a verified session, never from anything a model supplied."""

    department: str
    classification_max: str
    actor_id: str

    def allowed_levels(self) -> list[str]:
        return [level.lower() for level in LATTICE if not Classification.exceeds(level, self.classification_max)]


@dataclass(frozen=True)
class DeniedDocument:
    document_id: str
    classification: str
    acl: tuple[str, ...]
    reason: str
    matching_chunks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "classification": self.classification.upper(),
            "acl": list(self.acl),
            "reason": self.reason,
            "matching_chunks": self.matching_chunks,
        }


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    title: str
    version: int
    page: int
    bbox: Optional[list[float]]
    text: str
    classification: str
    acl: list[str]
    score: float = 0.0
    dense_rank: Optional[int] = None
    lexical_rank: Optional[int] = None
    evidence_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "version": self.version,
            "page": self.page,
            "bbox": self.bbox,
            "text": self.text,
            "classification": self.classification.upper(),
            "acl": self.acl,
            "score": round(self.score, 4),
            "dense_rank": self.dense_rank,
            "lexical_rank": self.lexical_rank,
        }


@dataclass
class SearchResult:
    query: str
    hits: list[SearchHit]
    denied: list[DeniedDocument]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            "denied": [d.to_dict() for d in self.denied],
            "denied_count": len(self.denied),
            "stats": self.stats,
        }


def query_terms(query: str) -> list[str]:
    seen: list[str] = []
    for token in _TOKEN.findall(query.lower()):
        if len(token) >= 2 and token not in _STOPWORDS and token not in seen:
            seen.append(token)
    return seen


_SELECT = (
    "SELECT c.id::text AS chunk_id, c.document_id::text AS document_id, d.title, c.version, c.page, "
    "c.bbox, c.content, c.classification, c.acl "
)
_FROM = "FROM document_chunks c JOIN documents d ON d.id = c.document_id AND d.version = c.version "
_PERMITTED = "c.classification = ANY(%(levels)s) AND %(dept)s = ANY(c.acl)"


def _row_to_hit(row: dict[str, Any]) -> SearchHit:
    return SearchHit(
        chunk_id=str(row["chunk_id"]),
        document_id=str(row["document_id"]),
        title=str(row["title"]),
        version=int(row["version"]),
        page=int(row["page"]),
        bbox=[float(v) for v in row["bbox"]] if row.get("bbox") else None,
        text=str(row["content"]),
        classification=str(row["classification"]),
        acl=[str(a) for a in row["acl"]],  # NOT NULL in the schema: never defaulted
    )


def _folder_clause(params: dict[str, Any], within: Optional[str], outside: Optional[str]) -> str:
    """Restrict to (or away from) one folder subtree. Folders only narrow what the
    permission predicate already allows -- they are never a grant."""
    clause = ""
    if within:
        params["fin"] = within.strip("/")
        clause += " AND (d.folder = %(fin)s OR starts_with(d.folder, %(fin)s || '/'))"
    if outside:
        params["fout"] = outside.strip("/")
        clause += " AND NOT (d.folder = %(fout)s OR starts_with(d.folder, %(fout)s || '/'))"
    return clause


def search(
    db: Database,
    gateway: Optional[Gateway],
    scope: SearchScope,
    query: str,
    *,
    top_k: int = 8,
    task_id: Optional[str] = None,
    within_folder: Optional[str] = None,
    outside_folder: Optional[str] = None,
) -> SearchResult:
    terms = query_terms(query)
    params: dict[str, Any] = {"levels": scope.allowed_levels(), "dept": scope.department, "k": _CANDIDATES}
    folders = _folder_clause(params, within_folder, outside_folder)
    stats: dict[str, Any] = {"terms": terms, "degraded": []}
    dense: list[SearchHit] = []
    lexical: list[SearchHit] = []
    max_distance: Optional[float] = None

    query_vector: Optional[list[float]] = None
    if gateway is not None:
        try:
            embedded = gateway.embed([query], classification=scope.classification_max, task_id=task_id, actor_id=scope.actor_id)
            query_vector = list(embedded.vectors[0])
            params["qvec"] = Vector(query_vector)
            params["model"] = embedded.model_id
            stats["embedding_model"] = embedded.model_id
        except (NoEligibleModel, ProviderError) as exc:
            stats["degraded"].append(f"dense retrieval unavailable ({str(exc)[:140]}); lexical only")
    else:
        stats["degraded"].append("no model gateway; lexical retrieval only")

    if query_vector is not None:
        rows = db.query(
            _SELECT + ", (c.embedding <=> %(qvec)s) AS distance " + _FROM
            + "WHERE d.status = 'ready' AND c.embedding IS NOT NULL AND c.embedding_model = %(model)s AND "
            + _PERMITTED + folders + " ORDER BY c.embedding <=> %(qvec)s LIMIT %(k)s",
            params,
        )
        for rank, row in enumerate(rows, start=1):
            hit = _row_to_hit(row)
            hit.dense_rank = rank
            dense.append(hit)
        if rows:
            max_distance = max(float(r["distance"]) for r in rows[: max(top_k, 1)])

    if terms:
        params["tsq"] = " | ".join(terms)
        rows = db.query(
            _SELECT + ", ts_rank_cd(c.tsv, to_tsquery('english', %(tsq)s)) AS lexical " + _FROM
            + "WHERE d.status = 'ready' AND c.tsv @@ to_tsquery('english', %(tsq)s) AND "
            + _PERMITTED + folders + " ORDER BY lexical DESC, c.id LIMIT %(k)s",
            params,
        )
        for rank, row in enumerate(rows, start=1):
            hit = _row_to_hit(row)
            hit.lexical_rank = rank
            lexical.append(hit)

    fused: dict[str, SearchHit] = {}
    for hit in dense + lexical:
        existing = fused.get(hit.chunk_id)
        if existing is None:
            fused[hit.chunk_id] = hit
        else:
            existing.dense_rank = existing.dense_rank or hit.dense_rank
            existing.lexical_rank = existing.lexical_rank or hit.lexical_rank
    for hit in fused.values():
        rrf = sum(1.0 / (_RRF_K + r) for r in (hit.dense_rank, hit.lexical_rank) if r is not None)
        lowered = hit.text.lower() + " " + hit.title.lower()
        coverage = sum(1 for t in terms if t in lowered) / len(terms) if terms else 0.0
        hit.score = rrf * _RRF_K / 2 + 0.5 * coverage
    ranked = sorted(fused.values(), key=lambda h: (-h.score, h.document_id, h.page, h.chunk_id))[:top_k]

    denied = _denied_documents(db, params, has_dense=query_vector is not None, has_terms=bool(terms),
                               max_distance=max_distance, folders=folders)
    stats.update(
        {
            "dense_candidates": len(dense),
            "lexical_candidates": len(lexical),
            "fusion": "reciprocal rank fusion",
            "rerank": "query-term coverage on CPU (cross-encoder not installed)",
            "allowed_levels": [lvl.upper() for lvl in scope.allowed_levels()],
            "department": scope.department,
            "within_folder": within_folder,
            "outside_folder": outside_folder,
        }
    )
    result = SearchResult(query=query, hits=ranked, denied=denied, stats=stats)
    if task_id is not None:
        register_evidence(db, task_id, ranked)
    return result


def _denied_documents(
    db: Database,
    params: dict[str, Any],
    *,
    has_dense: bool,
    has_terms: bool,
    max_distance: Optional[float],
    folders: str = "",
) -> list[DeniedDocument]:
    matches: list[str] = []
    if has_terms:
        matches.append("c.tsv @@ to_tsquery('english', %(tsq)s)")
    if has_dense and max_distance is not None:
        params = {**params, "maxdist": max_distance}
        matches.append("(c.embedding IS NOT NULL AND c.embedding_model = %(model)s AND (c.embedding <=> %(qvec)s) <= %(maxdist)s)")
    if not matches:
        return []
    rows = db.query(
        "SELECT c.document_id::text AS document_id, c.classification, c.acl, count(*) AS matching_chunks "
        + _FROM
        + "WHERE d.status = 'ready' AND NOT (" + _PERMITTED + ") AND (" + " OR ".join(matches) + ") " + folders + " "
        "GROUP BY c.document_id, c.classification, c.acl ORDER BY c.document_id",
        params,
    )
    allowed = set(params["levels"])
    return [
        DeniedDocument(
            document_id=str(r["document_id"]),
            classification=str(r["classification"]),
            acl=tuple(str(a) for a in r["acl"]),
            reason=REASON_CLASSIFICATION if str(r["classification"]) not in allowed else REASON_ACL,
            matching_chunks=int(r["matching_chunks"]),
        )
        for r in rows
    ]


def _evidence_lock(task_id: str) -> str:
    return f"citadel:evidence:{task_id}"


def register_evidence(db: Database, task_id: str, hits: Sequence[SearchHit]) -> None:
    """Give each hit a short, task-scoped evidence id (E1, E2, ...), reusing the id a
    chunk already has in this task, so a model cites `E3` rather than copying a UUID.

    Numbering happens inside one transaction holding a per-task lock: several agents of
    one task search at the same time, and two of them must never both hand out E5."""
    if not hits:
        return
    statements: list[tuple[str, Optional[Mapping[str, Any]]]] = []
    for hit in hits:
        statements.append((
            "INSERT INTO task_evidence (task_id, evidence_id, kind, chunk_id, document_id, version, page, bbox, "
            "text, classification, detail) SELECT %(t)s::uuid, 'E' || ((SELECT count(*) FROM task_evidence "
            "WHERE task_id = %(t)s::uuid AND kind = 'document') + 1), 'document', %(chunk)s::uuid, %(doc)s::uuid, "
            "%(v)s, %(page)s, %(bbox)s, %(text)s, %(c)s, %(detail)s WHERE NOT EXISTS (SELECT 1 FROM task_evidence "
            "WHERE task_id = %(t)s::uuid AND chunk_id = %(chunk)s::uuid)",
            {
                "t": task_id, "chunk": hit.chunk_id, "doc": hit.document_id,
                "v": hit.version, "page": hit.page,
                "bbox": RealArray(hit.bbox) if hit.bbox else None,
                "text": hit.text, "c": hit.classification, "detail": Json({"title": hit.title}),
            },
        ))
    rows = db.serialized(
        _evidence_lock(task_id),
        statements,
        (
            "SELECT chunk_id::text AS chunk_id, min(evidence_id) AS evidence_id FROM task_evidence "
            "WHERE task_id = %(t)s::uuid AND chunk_id = ANY(%(chunks)s::uuid[]) GROUP BY chunk_id",
            {"t": task_id, "chunks": [hit.chunk_id for hit in hits]},
        ),
    )
    assigned = {str(r["chunk_id"]): str(r["evidence_id"]) for r in rows}
    for hit in hits:
        hit.evidence_id = assigned.get(hit.chunk_id)


def register_reading(
    db: Database,
    task_id: str,
    *,
    document_id: str,
    version: int,
    page: int,
    bbox: Optional[Sequence[float]],
    text: str,
    classification: str,
    detail: Mapping[str, Any],
) -> str:
    """A region re-read during a task (the vision.extract tool) becomes citable evidence
    of its own, pinned to the same document, version, page and region as any chunk."""
    rows = db.serialized(
        _evidence_lock(task_id),
        [],
        (
            "INSERT INTO task_evidence (task_id, evidence_id, kind, document_id, version, page, bbox, text, "
            "classification, detail) VALUES (%(t)s::uuid, 'E' || ((SELECT count(*) FROM task_evidence "
            "WHERE task_id = %(t)s::uuid AND kind = 'document') + 1), 'document', %(d)s::uuid, %(v)s, %(p)s, "
            "%(b)s, %(x)s, %(c)s, %(detail)s) RETURNING evidence_id",
            {
                "t": task_id, "d": document_id, "v": version, "p": page,
                "b": RealArray(list(bbox)) if bbox else None, "x": text, "c": classification.lower(),
                "detail": Json(dict(detail)),
            },
        ),
    )
    return str(rows[0]["evidence_id"])


def register_computation(
    db: Database,
    task_id: str,
    *,
    text: str,
    classification: str,
    detail: Mapping[str, Any],
) -> str:
    """A computed value (calc.evaluate, code.run) becomes citable evidence -- C1, C2 --
    so a derived number in a deliverable traces to its recorded working, not to a
    model's arithmetic."""
    rows = db.serialized(
        _evidence_lock(task_id),
        [],
        (
            "INSERT INTO task_evidence (task_id, evidence_id, kind, text, classification, detail) VALUES "
            "(%(t)s::uuid, 'C' || ((SELECT count(*) FROM task_evidence WHERE task_id = %(t)s::uuid "
            "AND kind = 'computation') + 1), 'computation', %(x)s, %(c)s, %(detail)s) RETURNING evidence_id",
            {"t": task_id, "x": text, "c": classification.lower(), "detail": Json(dict(detail))},
        ),
    )
    return str(rows[0]["evidence_id"])


def task_evidence(db: Database, task_id: str) -> list[dict[str, Any]]:
    """Everything a task was given, in the order it was given -- for the UI's evidence
    panel and the provenance record."""
    return db.query(
        "SELECT e.evidence_id, e.kind, e.document_id::text AS document_id, d.title, e.version, e.page, e.bbox, "
        "e.text, e.classification, e.detail, e.created_at FROM task_evidence e "
        "LEFT JOIN documents d ON d.id = e.document_id WHERE e.task_id = %(t)s::uuid "
        "ORDER BY e.created_at, e.evidence_id",
        {"t": task_id},
    )


def evidence(db: Database, task_id: str, evidence_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Resolve evidence ids for one task. Ids the task was never given are absent."""
    if not evidence_ids:
        return {}
    rows = db.query(
        "SELECT e.evidence_id, e.kind, e.chunk_id::text AS chunk_id, e.document_id::text AS document_id, "
        "e.version, e.page, e.bbox, e.text, e.classification, e.detail, d.title "
        "FROM task_evidence e LEFT JOIN documents d ON d.id = e.document_id "
        "WHERE e.task_id = %(t)s::uuid AND e.evidence_id = ANY(%(ids)s)",
        {"t": task_id, "ids": list(evidence_ids)},
    )
    return {str(r["evidence_id"]): r for r in rows}


def read_page(
    db: Database,
    scope: SearchScope,
    document_id: str,
    *,
    page: Optional[int] = None,
    version: Optional[int] = None,
    task_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """One page (default: the first) of one document, if the caller may see it --
    the same permission predicate as search, in the same statement as the read."""
    if not is_uuid(document_id):
        return None
    params = {"id": document_id, "levels": scope.allowed_levels(), "dept": scope.department}
    document = db.query_one(
        "SELECT id::text AS id, title, filename, classification, acl, version, page_count, status, ingest_report, "
        "folder FROM documents WHERE id = %(id)s::uuid AND classification = ANY(%(levels)s) AND %(dept)s = ANY(acl)",
        params,
    )
    if document is None:
        return None
    target_version = version or int(document["version"])
    if target_version != int(document["version"]):
        pages = db.scalar(
            "SELECT count(*) FROM document_pages WHERE document_id = %(id)s::uuid AND version = %(v)s",
            {"id": document_id, "v": target_version},
        )
        document = {**document, "page_count": int(pages or 0)}
    target_page = page or 1
    page_row = db.query_one(
        "SELECT page, width_px, height_px, text_source, ocr_confidence, vision, image_ref IS NOT NULL AS has_image "
        "FROM document_pages WHERE document_id = %(id)s::uuid AND version = %(v)s AND page = %(p)s",
        {"id": document_id, "v": target_version, "p": target_page},
    )
    blocks = db.query(
        "SELECT id::text AS id, block_index, kind, source, text, bbox, confidence, low_confidence "
        "FROM document_blocks WHERE document_id = %(id)s::uuid AND version = %(v)s AND page = %(p)s "
        "ORDER BY block_index",
        {"id": document_id, "v": target_version, "p": target_page},
    )
    chunks: list[SearchHit] = []
    if task_id is not None:
        # Any version the caller reads is citable -- an earlier issue of a policy is as
        # much evidence of what it said then as the current one is of what it says now.
        for row in _version_chunks(db, params, target_version, target_page):
            chunks.append(_row_to_hit(row))
        register_evidence(db, task_id, chunks)
    return {
        "document": document,
        "version": target_version,
        "page": page_row,
        "blocks": blocks,
        "evidence": [{"evidence_id": c.evidence_id, "text": c.text, "bbox": c.bbox} for c in chunks],
    }


def visible_documents(db: Database, scope: SearchScope) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id::text AS id, title, filename, mime_type, classification, acl, status, page_count, "
        "uploaded_by, version, ingest_report, error, created_at, folder FROM documents "
        "WHERE classification = ANY(%(levels)s) AND %(dept)s = ANY(acl) ORDER BY folder, title",
        {"levels": scope.allowed_levels(), "dept": scope.department},
    )


def _version_chunks(db: Database, params: Mapping[str, Any], version: int, page: Optional[int]) -> list[dict[str, Any]]:
    """The chunks of one version of one document the caller may see (the same predicate
    as search), whether or not that version is the current one."""
    page_clause = " AND c.page = %(p)s" if page is not None else ""
    return db.query(
        _SELECT + "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.document_id = %(id)s::uuid AND c.version = %(v)s" + page_clause + " AND " + _PERMITTED
        + " ORDER BY c.page, c.chunk_index",
        {**params, "v": version, "p": page},
    )


def document_versions(db: Database, scope: SearchScope, document_id: str) -> list[dict[str, Any]]:
    """Every issue of a document the caller may see, oldest first."""
    if not is_uuid(document_id):
        return []
    return db.query(
        "SELECT v.version, v.filename, v.change_note, v.effective, v.uploaded_by, v.created_at, "
        "(v.version = d.version) AS current, "
        "(SELECT count(*) FROM document_pages p WHERE p.document_id = v.document_id AND p.version = v.version) AS pages "
        "FROM document_versions v JOIN documents d ON d.id = v.document_id "
        "WHERE v.document_id = %(id)s::uuid AND d.classification = ANY(%(levels)s) AND %(dept)s = ANY(d.acl) "
        "ORDER BY v.version",
        {"id": document_id, "levels": scope.allowed_levels(), "dept": scope.department},
    )


_SENTENCE = re.compile(r"(?<=[.;:])\s+|\n+")


def _sentences(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """(normalised sentence, index of the chunk it came from) in reading order."""
    out: list[tuple[str, int]] = []
    for index, row in enumerate(rows):
        for part in _SENTENCE.split(str(row["content"])):
            text = " ".join(part.split())
            if len(text) >= 3:
                out.append((text, index))
    return out


def diff_versions(
    db: Database,
    scope: SearchScope,
    document_id: str,
    *,
    from_version: int,
    to_version: int,
    task_id: Optional[str] = None,
    limit: int = 30,
) -> Optional[dict[str, Any]]:
    """What changed between two issues of a document, sentence by sentence, each change
    pinned to the chunk (and so the page and region) it sits in -- and, inside a task,
    to citable evidence ids for the old and the new wording. Deterministic: the model
    reads a diff rather than being trusted to spot one."""
    import difflib

    if not is_uuid(document_id):
        return None
    params = {"id": document_id, "levels": scope.allowed_levels(), "dept": scope.department}
    document = db.query_one(
        "SELECT id::text AS id, title, version FROM documents WHERE id = %(id)s::uuid "
        "AND classification = ANY(%(levels)s) AND %(dept)s = ANY(acl)",
        params,
    )
    if document is None:
        return None
    old_rows = _version_chunks(db, params, from_version, None)
    new_rows = _version_chunks(db, params, to_version, None)
    if not old_rows or not new_rows:
        return {"document": document, "from_version": from_version, "to_version": to_version, "changes": [],
                "note": "one of the versions has no readable text"}
    old_sentences, new_sentences = _sentences(old_rows), _sentences(new_rows)
    matcher = difflib.SequenceMatcher(a=[t for t, _ in old_sentences], b=[t for t, _ in new_sentences], autojunk=False)
    changes: list[dict[str, Any]] = []
    cited_old: dict[int, SearchHit] = {}
    cited_new: dict[int, SearchHit] = {}
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "equal":
            continue
        removed = old_sentences[a0:a1]
        added = new_sentences[b0:b1]
        for text, chunk_index in removed:
            cited_old.setdefault(chunk_index, _row_to_hit(old_rows[chunk_index]))
        for text, chunk_index in added:
            cited_new.setdefault(chunk_index, _row_to_hit(new_rows[chunk_index]))
        changes.append({
            "change": {"replace": "changed", "delete": "removed", "insert": "added"}[op],
            "before": " ".join(t for t, _ in removed) or None,
            "after": " ".join(t for t, _ in added) or None,
            "_old_chunks": sorted({i for _, i in removed}),
            "_new_chunks": sorted({i for _, i in added}),
        })
        if len(changes) >= limit:
            break
    if task_id is not None:
        register_evidence(db, task_id, [*cited_old.values(), *cited_new.values()])
    for change in changes:
        change["before_evidence"] = [cited_old[i].evidence_id for i in change.pop("_old_chunks") if i in cited_old]
        change["after_evidence"] = [cited_new[i].evidence_id for i in change.pop("_new_chunks") if i in cited_new]
    return {
        "document": document,
        "from_version": from_version,
        "to_version": to_version,
        "changes": changes,
        "unchanged_sentences": sum(a1 - a0 for op, a0, a1, _, _ in matcher.get_opcodes() if op == "equal"),
    }


def document_facts(db: Database, document_id: str) -> Optional[dict[str, Any]]:
    """Classification and ACL of a document, whoever is asking -- for the chokepoint's
    trusted resource resolution only, never returned to a caller."""
    if not is_uuid(document_id):
        return None
    return db.query_one(
        "SELECT id::text AS id, classification, acl, version, status FROM documents WHERE id = %(id)s::uuid",
        {"id": document_id},
    )


__all__ = [
    "LATTICE",
    "is_uuid",
    "SearchScope",
    "DeniedDocument",
    "SearchHit",
    "SearchResult",
    "query_terms",
    "search",
    "register_evidence",
    "register_reading",
    "register_computation",
    "task_evidence",
    "evidence",
    "read_page",
    "visible_documents",
    "document_versions",
    "diff_versions",
    "document_facts",
    "REASON_CLASSIFICATION",
    "REASON_ACL",
]
