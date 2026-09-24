"""Documents: the upload door, the corpus a person may see, page reads with regions,
page images for citation highlights, and interactive search with its denial record.

Every read here uses the same permission predicate the knowledge package applies in
SQL, built from the verified session -- a person sees exactly what their department and
clearance allow, and what they may not see is counted and explained, never shown.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from citadel_knowledge import (
    SearchScope,
    UploadRejected,
    diff_versions,
    document_versions,
    read_page,
    register_new_version,
    register_upload,
    search,
    validate_metadata,
    visible_documents,
)
from citadel_knowledge.retrieval import is_uuid

from citadel_api.common import blocking, effective_level, error, json_body, state
from citadel_api.deps import require_user

MAX_UPLOAD = 50 * 1024 * 1024


def _scope(user: Any, level: str | None = None) -> SearchScope:
    return SearchScope(user.department, effective_level(user, level), user.user_id)


async def documents_index(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    rows = await blocking(visible_documents, app.db, _scope(user))
    return JSONResponse({"documents": rows, "scope": {"department": user.department, "clearance": user.clearance.upper()}})


async def upload_document(request: Request) -> Response:
    """Multipart upload. Classification and ACL are mandatory at the door, checked
    against the profile ceiling and the uploader's own clearance and department."""
    app = state(request)
    user = require_user(request, app)
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return error("a file is required (multipart field 'file')")
    content = await upload.read()
    metadata = {
        key: form.get(key)
        for key in ("classification", "acl", "title", "folder")
        if isinstance(form.get(key), str) and str(form.get(key)).strip()
    }
    note = _form_text(form, "change_note")
    effective = _form_text(form, "effective")
    filename = upload.filename or "upload"

    def door() -> dict[str, Any]:
        meta = validate_metadata(metadata, filename=filename,
                                 profile_ceiling=app.registry.profile.classification_ceiling, uploader=user)
        return register_upload(app.db, app.data_dir, filename=filename, content=content, metadata=meta, uploader=user,
                               change_note=note, effective=effective)

    try:
        row = await blocking(door)
    except UploadRejected as exc:
        await blocking(app.audit.record, "document.rejected", actor_id=user.user_id,
                       payload={"stage": "upload", "code": exc.code, "filename": filename[:200]})
        return error(exc.message, 400, code=exc.code)
    return JSONResponse(row, status_code=201)


def _form_text(form: Any, key: str) -> str | None:
    value = form.get(key)
    return " ".join(value.split())[:300] or None if isinstance(value, str) else None


async def document_page(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    document_id = request.path_params["document_id"]
    page = int(request.path_params.get("page") or 1)
    raw_version = request.query_params.get("version")
    version = int(raw_version) if raw_version and raw_version.isdigit() else None
    found = await blocking(read_page, app.db, _scope(user), document_id, page=page, version=version)
    if found is None:
        return error("no such document, or not one you may see", 404)
    return JSONResponse(found)


async def document_history(request: Request) -> Response:
    """Every issue of a document the person may see, oldest first."""
    app = state(request)
    user = require_user(request, app)
    versions = await blocking(document_versions, app.db, _scope(user), request.path_params["document_id"])
    if not versions:
        return error("no such document, or not one you may see", 404)
    return JSONResponse({"versions": versions})


async def document_diff(request: Request) -> Response:
    """What changed between two issues, sentence by sentence (defaults: the previous
    issue against the current one) -- the same deterministic diff docs.diff gives agents."""
    app = state(request)
    user = require_user(request, app)
    document_id = request.path_params["document_id"]
    scope = _scope(user)

    def compare() -> dict[str, Any] | None:
        versions = document_versions(app.db, scope, document_id)
        if not versions:
            return None
        current = max(int(v["version"]) for v in versions)
        to_raw, from_raw = request.query_params.get("to"), request.query_params.get("from")
        to_version = int(to_raw) if to_raw and to_raw.isdigit() else current
        from_version = int(from_raw) if from_raw and from_raw.isdigit() else max(1, to_version - 1)
        if from_version == to_version:
            return {"versions": versions, "changes": [], "note": "only one issue of this document exists"}
        result = diff_versions(app.db, scope, document_id, from_version=from_version, to_version=to_version)
        return None if result is None else {**result, "versions": versions}

    found = await blocking(compare)
    return JSONResponse(found) if found is not None else error("no such document, or not one you may see", 404)


async def reissue_document(request: Request) -> Response:
    """A new issue of an existing document (multipart 'file', optional 'change_note' and
    'effective'): same classification and ACL, next version number, ingested like any
    upload. Earlier issues stay readable and citable."""
    app = state(request)
    user = require_user(request, app)
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return error("a file is required (multipart field 'file')")
    content = await upload.read()
    if len(content) > MAX_UPLOAD:
        return error("the file is larger than 50 MB")
    note, effective = _form_text(form, "change_note"), _form_text(form, "effective")

    def door() -> dict[str, Any]:
        return register_new_version(app.db, app.data_dir, document_id=request.path_params["document_id"],
                                    filename=upload.filename or "upload", content=content, uploader=user,
                                    change_note=note, effective=effective)

    try:
        return JSONResponse(await blocking(door), status_code=201)
    except UploadRejected as exc:
        return error(exc.message, 404 if exc.code == "not_found" else 400, code=exc.code)


async def document_page_image(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    document_id = request.path_params["document_id"]
    if not is_uuid(document_id):
        return error("no such document", 404)
    scope = _scope(user)
    row = await blocking(
        app.db.query_one,
        "SELECT p.image_ref FROM document_pages p JOIN documents d ON d.id = p.document_id AND d.version = p.version "
        "WHERE d.id = %(d)s::uuid AND p.page = %(p)s AND d.classification = ANY(%(levels)s) AND %(dept)s = ANY(d.acl)",
        {"d": document_id, "p": int(request.path_params["page"]), "levels": scope.allowed_levels(), "dept": scope.department},
    )
    if row is None or not row.get("image_ref"):
        return error("no page image, or not one you may see", 404)
    return FileResponse(str(app.data_dir.resolve(str(row["image_ref"]))), media_type="image/jpeg")


async def search_documents(request: Request) -> Response:
    """Interactive search, as the signed-in person: the ACL demonstration surface."""
    app = state(request)
    user = require_user(request, app)
    body = await json_body(request)
    query = str(body.get("query") or "").strip()
    if not query:
        return error("a query is required")
    top_k = max(1, min(int(body.get("top_k") or 8), 30))
    scope = _scope(user, body.get("classification"))
    result = await blocking(search, app.db, app.gateway, scope, query, top_k=top_k)
    for denied in result.denied:
        await blocking(app.audit.record, "retrieval.denied_doc", actor_id=user.user_id,
                       payload={"surface": "search", **denied.to_dict()})
    return JSONResponse(result.to_dict())


__all__ = [
    "documents_index",
    "upload_document",
    "document_page",
    "document_page_image",
    "document_history",
    "document_diff",
    "reissue_document",
    "search_documents",
]
