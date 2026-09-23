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

from citadel_knowledge import SearchScope, UploadRejected, read_page, register_upload, search, validate_metadata, visible_documents
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
        for key in ("classification", "acl", "title")
        if isinstance(form.get(key), str) and str(form.get(key)).strip()
    }
    filename = upload.filename or "upload"

    def door() -> dict[str, Any]:
        meta = validate_metadata(metadata, filename=filename,
                                 profile_ceiling=app.registry.profile.classification_ceiling, uploader=user)
        return register_upload(app.db, app.data_dir, filename=filename, content=content, metadata=meta, uploader=user)

    try:
        row = await blocking(door)
    except UploadRejected as exc:
        await blocking(app.audit.record, "document.rejected", actor_id=user.user_id,
                       payload={"stage": "upload", "code": exc.code, "filename": filename[:200]})
        return error(exc.message, 400, code=exc.code)
    return JSONResponse(row, status_code=201)


async def document_page(request: Request) -> Response:
    app = state(request)
    user = require_user(request, app)
    document_id = request.path_params["document_id"]
    page = int(request.path_params.get("page") or 1)
    found = await blocking(read_page, app.db, _scope(user), document_id, page=page)
    if found is None:
        return error("no such document, or not one you may see", 404)
    return JSONResponse(found)


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


__all__ = ["documents_index", "upload_document", "document_page", "document_page_image", "search_documents"]
