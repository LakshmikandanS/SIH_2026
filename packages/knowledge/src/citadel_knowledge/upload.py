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
from typing import Any, Mapping, Sequence

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User
from citadel_platform.db import Database
from citadel_platform.storage import DataDir, safe_filename

from citadel_knowledge.extract import UnsupportedDocument, detect_kind

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEPARTMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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
    return UploadMetadata(classification=level, acl=tuple(acl), title=title or filename)


def register_upload(
    db: Database,
    data_dir: DataDir,
    *,
    filename: str,
    content: bytes,
    metadata: UploadMetadata,
    uploader: User,
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
    row = db.query_one(
        "INSERT INTO documents (id, title, filename, mime_type, classification, acl, uploaded_by, "
        "sha256, storage_ref) VALUES (%(id)s::uuid, %(title)s, %(filename)s, %(mime)s, %(classification)s, "
        "%(acl)s, %(uploader)s, %(sha)s, %(ref)s) RETURNING id::text AS id, title, status, classification, acl",
        {
            "id": document_id,
            "title": metadata.title[:300],
            "filename": safe_filename(filename),
            "mime": mime,
            "classification": metadata.classification.lower(),
            "acl": list(metadata.acl),
            "uploader": uploader.user_id,
            "sha": hashlib.sha256(content).hexdigest(),
            "ref": data_dir.relative(path),
        },
    )
    assert row is not None
    return row


__all__ = ["UploadRejected", "UploadMetadata", "validate_metadata", "register_upload", "MAX_UPLOAD_BYTES"]
