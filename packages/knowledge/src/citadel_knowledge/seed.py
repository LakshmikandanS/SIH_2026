"""Seeding the demonstration corpus through the ordinary upload door.

`ops/demo/corpus/manifest.yaml` lists each file with its classification, ACL and
uploader. Seeding does exactly what a person uploading those files would: the same
validation (mandatory classification and ACL, profile ceiling, uploader clearance and
department), the same pending row, the same worker ingestion. It is idempotent --
a file whose bytes are already present is skipped -- so it can run on every start.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from citadel_contracts.domain import User
from citadel_platform.db import Database
from citadel_platform.storage import DataDir

from citadel_knowledge.upload import UploadRejected, register_upload, validate_metadata


def seed_corpus(
    db: Database,
    data_dir: DataDir,
    manifest_path: Path,
    *,
    profile_ceiling: str,
    lookup_user: Callable[[str], Optional[User]],
) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    created: list[str] = []
    skipped: list[str] = []
    rejected: list[dict[str, str]] = []
    for entry in manifest.get("documents") or []:
        path = manifest_path.parent / str(entry["file"])
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if db.scalar("SELECT count(*) FROM documents WHERE sha256 = %(s)s", {"s": digest}):
            skipped.append(str(entry["file"]))
            continue
        uploader = lookup_user(str(entry["uploaded_by"]))
        if uploader is None:
            rejected.append({"file": str(entry["file"]), "reason": f"no such user {entry['uploaded_by']!r}"})
            continue
        try:
            metadata = validate_metadata(entry, filename=path.name, profile_ceiling=profile_ceiling, uploader=uploader)
            register_upload(db, data_dir, filename=path.name, content=content, metadata=metadata, uploader=uploader)
            created.append(str(entry["file"]))
        except UploadRejected as exc:
            rejected.append({"file": str(entry["file"]), "reason": f"{exc.code}: {exc.message}"})
    return {"created": created, "skipped": skipped, "rejected": rejected}


__all__ = ["seed_corpus"]
