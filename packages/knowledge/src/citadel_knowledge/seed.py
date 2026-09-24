"""Register the demonstration corpus through the same door a person's upload uses.

Every manifest entry declares its classification and ACL explicitly and passes
`validate_metadata` exactly as an upload does; nothing here can file a document the
named uploader could not have filed themselves.

An entry with `version_of: <key>` re-issues an earlier entry as a new version of the
same document (a policy revised last month, say). A re-issue waits until the version
before it has been ingested -- ingestion reads a document's current version, so filing
both at once would skip the first -- and the caller simply calls again later; each call
does whatever has become possible and reports what is still waiting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from citadel_contracts.domain import User
from citadel_platform.db import Database
from citadel_platform.storage import DataDir

from citadel_knowledge.upload import (
    UploadRejected,
    normalise_folder,
    register_new_version,
    register_upload,
    validate_metadata,
)


def _known_version(db: Database, digest: str) -> Optional[dict[str, Any]]:
    return db.query_one(
        "SELECT v.document_id::text AS document_id, v.version, d.status, d.version AS current_version "
        "FROM document_versions v JOIN documents d ON d.id = v.document_id WHERE v.sha256 = %(s)s "
        "ORDER BY v.version DESC LIMIT 1",
        {"s": digest},
    )


def _file_if_unfiled(db: Database, document_id: str, folder: Any) -> None:
    """An installation seeded before documents had folders: file the document where the
    manifest now says -- only if nobody has filed it anywhere since."""
    try:
        target = normalise_folder(folder)
    except UploadRejected:
        return
    if target:
        db.execute(
            "UPDATE documents SET folder = %(f)s WHERE id = %(id)s::uuid AND coalesce(folder, '') = ''",
            {"f": target, "id": document_id},
        )


def seed_corpus(
    db: Database,
    data_dir: DataDir,
    manifest_path: Path,
    *,
    profile_ceiling: str,
    lookup_user: Callable[[str], Optional[User]],
) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entries = list(manifest.get("documents") or [])
    digests: dict[str, str] = {}
    for entry in entries:
        digests[str(entry["file"])] = hashlib.sha256((manifest_path.parent / str(entry["file"])).read_bytes()).hexdigest()
    by_key = {str(entry["key"]): entry for entry in entries if entry.get("key")}

    created: list[str] = []
    versioned: list[str] = []
    skipped: list[str] = []
    waiting: list[str] = []
    rejected: list[dict[str, str]] = []

    for entry in entries:
        name = str(entry["file"])
        path = manifest_path.parent / name
        digest = digests[name]
        known = _known_version(db, digest)
        if known is not None:
            if entry.get("folder") and not entry.get("version_of"):
                _file_if_unfiled(db, str(known["document_id"]), entry["folder"])
            skipped.append(name)
            continue
        uploader = lookup_user(str(entry["uploaded_by"]))
        if uploader is None:
            rejected.append({"file": name, "reason": f"no such user {entry['uploaded_by']!r}"})
            continue
        try:
            if entry.get("version_of"):
                base = by_key.get(str(entry["version_of"]))
                if base is None:
                    rejected.append({"file": name, "reason": f"version_of names no entry {entry['version_of']!r}"})
                    continue
                previous = _known_version(db, digests[str(base["file"])])
                if previous is None or previous["status"] != "ready":
                    waiting.append(name)
                    continue
                # Re-issues chain: wait for every earlier issue of this document to be in.
                register_new_version(
                    db, data_dir, document_id=str(previous["document_id"]), filename=path.name,
                    content=path.read_bytes(), uploader=uploader,
                    change_note=str(entry["change_note"]) if entry.get("change_note") else None,
                    effective=str(entry["effective"]) if entry.get("effective") else None,
                )
                versioned.append(name)
                continue
            metadata = validate_metadata(entry, filename=path.name, profile_ceiling=profile_ceiling, uploader=uploader)
            register_upload(
                db, data_dir, filename=path.name, content=path.read_bytes(), metadata=metadata, uploader=uploader,
                change_note=str(entry["change_note"]) if entry.get("change_note") else None,
                effective=str(entry["effective"]) if entry.get("effective") else None,
            )
            created.append(name)
        except UploadRejected as exc:
            if exc.code == "busy":
                waiting.append(name)
            else:
                rejected.append({"file": name, "reason": f"{exc.code}: {exc.message}"})
    return {"created": created, "versioned": versioned, "skipped": skipped, "waiting": waiting, "rejected": rejected}


__all__ = ["seed_corpus"]
