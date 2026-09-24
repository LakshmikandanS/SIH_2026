"""The door: classification and ACL are mandatory, checked before a byte is kept.

packages/knowledge/AGENTS.md: "A document without classification and ACL metadata is
rejected, loudly. Never defaulted." Five checks, all before anything is written:

1. classification present; 2. ACL present and non-empty;
3. the marking is in the lattice (an unknown marking is not comparable -- fail closed);
4. it does not exceed the active profile's `classification_ceiling` -- the mechanical
   form of invariant 11: `hpc-eval`'s ceiling is PUBLIC, so that deployment *cannot*
   hold confidential material, whoever tries;
5. it does not exceed the uploader's own clearance, and the uploader's department is
   in the ACL -- nobody can file a document they would not themselves be allowed to see.

`tests/structural/test_profile_ceiling_enforced_at_ingest.py` keeps the defaulting idiom
out of this package entirely.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User
from citadel_platform.db import Database
from citadel_platform.storage import DataDir, safe_filename

from citadel_knowledge.extract import UnsupportedDocument, detect_kind

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEPARTMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: A folder is where people find a document, never what may see it (the ACL decides
#: that): slash-separated segments of letters, digits, spaces, dots, dashes, underscores.
_FOLDER_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")


class UploadRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadMetadata:
    classification: str  # uppercase, lattice-valid
    acl: tuple[str, ...]
    title: str
    folder: str = ""


def normalise_folder(raw: Any) -> str:
    """'MANUFACTURING_DEPT / sector1/' -> 'MANUFACTURING_DEPT/sector1'. Rejects anything
    that is not a plain relative path of simple segments ('..' included)."""
    if raw is None:
        return ""
    segments = [part.strip() for part in str(raw).replace("\\", "/").split("/") if part.strip()]
    if len(segments) > 6:
        raise UploadRejected("folder_invalid", "folders are at most six levels deep")
    for segment in segments:
        if segment in (".", "..") or not _FOLDER_SEGMENT.match(segment):
            raise UploadRejected("folder_invalid", f"folder segment {segment!r} is not a plain name")
    return "/".join(segments)


def _acl_entries(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items: Sequence[Any] = [part for part in re.split(r"[,\s]+", raw) if part]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise UploadRejected("acl_missing", "ACL must be a list of departments")
    entries = sorted({str(item).strip().lower() for item in items if str(item).strip()})
    for entry in entries:
        if not _DEPARTMENT.match(entry):
            raise UploadRejected("acl_invalid", f"ACL entry {entry!r} is not a department identifier")
    return entries


def validate_metadata(
    metadata: Mapping[str, Any],
    *,
    filename: str,
    profile_ceiling: str,
    uploader: User,
) -> UploadMetadata:
    if "classification" not in metadata or not str(metadata["classification"]).strip():
        raise UploadRejected(
            "classification_missing",
            "every document must declare a classification at upload -- it is never defaulted",
        )
    if "acl" not in metadata or not metadata["acl"]:
        raise UploadRejected("acl_missing", "every document must declare an ACL (departments allowed to see it)")

    level = str(metadata["classification"]).strip().upper()
    try:
        Classification.rank(level)
    except ValueError:
        raise UploadRejected(
            "unknown_classification",
            f"{metadata['classification']!r} is not a classification this system recognises; unknown markings are refused",
        ) from None
    if Classification.exceeds(level, profile_ceiling):
        raise UploadRejected(
            "exceeds_profile_ceiling",
            f"this deployment's profile holds nothing above {profile_ceiling}; a {level} document cannot be ingested here",
        )
    clearance = uploader.clearance.upper()
    if Classification.exceeds(level, clearance):
        raise UploadRejected(
            "exceeds_uploader_clearance",
            f"you are cleared to {clearance}; you cannot file a {level} document",
        )
    acl = _acl_entries(metadata["acl"])
    if not acl:
        raise UploadRejected("acl_missing", "the ACL names no department")
    if uploader.department not in acl:
        raise UploadRejected(
            "uploader_not_in_acl",
            f"your department ({uploader.department}) must be in the ACL -- you would not be able to see this document",
        )
    title = str(metadata["title"]).strip() if "title" in metadata and metadata["title"] else ""
    folder = normalise_folder(metadata["folder"]) if "folder" in metadata else ""
    return UploadMetadata(classification=level, acl=tuple(acl), title=title or filename, folder=folder)


def register_upload(
    db: Database,
    data_dir: DataDir,
    *,
    filename: str,
    content: bytes,
    metadata: UploadMetadata,
    uploader: User,
    change_note: Optional[str] = None,
    effective: Optional[str] = None,
) -> dict[str, Any]:
    """Keep the bytes and create the `pending` row a worker will ingest."""
    if not content:
        raise UploadRejected("empty_file", "the uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadRejected("too_large", f"uploads are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        _kind, mime = detect_kind(filename, content[:16])
    except UnsupportedDocument as exc:
        raise UploadRejected("unsupported_type", str(exc)) from None

    document_id = str(uuid.uuid4())
    directory = data_dir.document_dir(document_id, 1)
    path = directory / ("original_" + safe_filename(filename))
    path.write_bytes(content)
    params = {
        "id": document_id,
        "title": metadata.title[:300],
        "filename": safe_filename(filename),
        "mime": mime,
        "classification": metadata.classification.lower(),
        "acl": list(metadata.acl),
        "uploader": uploader.user_id,
        "sha": hashlib.sha256(content).hexdigest(),
        "ref": data_dir.relative(path),
        "folder": metadata.folder,
        "note": change_note,
        "effective": effective,
    }
    db.script([
        ("INSERT INTO documents (id, title, filename, mime_type, classification, acl, uploaded_by, sha256, "
         "storage_ref, folder) VALUES (%(id)s::uuid, %(title)s, %(filename)s, %(mime)s, %(classification)s, "
         "%(acl)s, %(uploader)s, %(sha)s, %(ref)s, %(folder)s)", params),
        ("INSERT INTO document_versions (document_id, version, filename, sha256, storage_ref, uploaded_by, "
         "change_note, effective) VALUES (%(id)s::uuid, 1, %(filename)s, %(sha)s, %(ref)s, %(uploader)s, "
         "%(note)s, %(effective)s::date)", params),
    ])
    row = db.query_one(
        "SELECT id::text AS id, title, status, classification, acl, folder, version FROM documents "
        "WHERE id = %(id)s::uuid",
        {"id": document_id},
    )
    assert row is not None
    return row


def register_new_version(
    db: Database,
    data_dir: DataDir,
    *,
    document_id: str,
    filename: str,
    content: bytes,
    uploader: User,
    change_note: Optional[str] = None,
    effective: Optional[str] = None,
) -> dict[str, Any]:
    """Re-issue a document. The new version keeps the document's classification and
    ACL -- a re-issue is not a way to reclassify -- and the uploader must be allowed to
    see the document they are re-issuing. Every earlier version stays readable."""
    if not content:
        raise UploadRejected("empty_file", "the uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadRejected("too_large", f"uploads are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    current = db.query_one(
        "SELECT id::text AS id, version, classification, acl, status FROM documents WHERE id = %(id)s::uuid",
        {"id": document_id},
    )
    if current is None:
        raise UploadRejected("no_such_document", "there is no such document to re-issue")
    if Classification.exceeds(str(current["classification"]).upper(), uploader.clearance.upper()) or (
        uploader.department not in list(current["acl"])
    ):
        raise UploadRejected("not_permitted", "you may only re-issue a document you are allowed to see")
    if current["status"] in ("pending", "processing"):
        raise UploadRejected("busy", "the current version is still being ingested; re-issue it once that finishes")
    try:
        _kind, mime = detect_kind(filename, content[:16])
    except UnsupportedDocument as exc:
        raise UploadRejected("unsupported_type", str(exc)) from None
    version = int(current["version"]) + 1
    path = data_dir.document_dir(document_id, version) / ("original_" + safe_filename(filename))
    path.write_bytes(content)
    params = {
        "id": document_id, "v": version, "filename": safe_filename(filename), "mime": mime,
        "sha": hashlib.sha256(content).hexdigest(), "ref": data_dir.relative(path), "uploader": uploader.user_id,
        "note": change_note, "effective": effective,
    }
    db.script([
        ("INSERT INTO document_versions (document_id, version, filename, sha256, storage_ref, uploaded_by, "
         "change_note, effective) VALUES (%(id)s::uuid, %(v)s, %(filename)s, %(sha)s, %(ref)s, %(uploader)s, "
         "%(note)s, %(effective)s::date)", params),
        ("UPDATE documents SET version = %(v)s, filename = %(filename)s, mime_type = %(mime)s, sha256 = %(sha)s, "
         "storage_ref = %(ref)s, status = 'pending', page_count = NULL, error = NULL, ingest_report = '{}' "
         "WHERE id = %(id)s::uuid", params),
    ])
    row = db.query_one(
        "SELECT id::text AS id, title, status, classification, acl, folder, version FROM documents "
        "WHERE id = %(id)s::uuid",
        {"id": document_id},
    )
    assert row is not None
    return row


__all__ = [
    "UploadRejected",
    "UploadMetadata",
    "validate_metadata",
    "normalise_folder",
    "register_upload",
    "register_new_version",
    "MAX_UPLOAD_BYTES",
]
