"""The mandatory ACL sidecar (design doc section 6.9).

    "Every source document must ship with a sidecar metadata file. Ingestion
     rejects outright any document missing one ... Do not invent a fallback
     classification or a 'public by default' behavior. Missing sidecar =
     ingestion fails for that document, full stop."

This module is the one place that rule is enforced. It does exactly one
thing: given a document path, find and validate its `<name>.meta.json`, or
raise -- never return a default, never infer anything from the document's
content or its file path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from app.db.state_machines import Classification
from app.rag import settings

#: Every field a sidecar must carry. `classification` and `acl` are what
#: section 6.9 calls mandatory; `department`, `document_id` and
#: `document_version` are required too because every sidecar actually shipped
#: with the demo corpus carries all five (see data/README.md) and inventing a
#: fallback for any of them would be exactly the "default classification"
#: behaviour section 6.9 forbids.
REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"classification", "acl", "department", "document_id", "document_version"}
)


class SidecarError(Exception):
    """Base class for "this document cannot be ingested as-is"."""


class MissingSidecarError(SidecarError):
    """No `<document>.meta.json` exists next to the document at all.

    Design doc section 6.9, verbatim: ingestion "rejects outright" a document
    in this state. There is no fallback classification and no silent skip --
    this exception IS the rejection, and the caller (`app.rag.ingest`) is
    required to let it propagate rather than catch-and-continue.
    """

    def __init__(self, document_path: Path) -> None:
        meta_path = sidecar_path(document_path)
        super().__init__(
            f"{document_path} has no sidecar metadata file at {meta_path}; "
            f"design doc section 6.9 requires ingestion to reject this "
            f"document outright -- no default classification, no inferred ACL"
        )
        self.document_path = document_path
        self.meta_path = meta_path


class InvalidSidecarError(SidecarError):
    """A sidecar exists but is not usable: bad JSON, a missing required
    field, or a classification the lattice does not recognise.

    Also a hard rejection -- a sidecar that cannot be trusted is exactly as
    dangerous as one that does not exist, and guessing at the missing piece
    would reintroduce the "default classification" behaviour section 6.9
    forbids.
    """

    def __init__(self, meta_path: Path, reason: str) -> None:
        super().__init__(f"sidecar {meta_path} is invalid: {reason}")
        self.meta_path = meta_path
        self.reason = reason


def sidecar_path(document_path: Path) -> Path:
    """`data/x/doc.txt` -> `data/x/doc.txt.meta.json` (section 6.9's shape:
    the sidecar name is the document's full filename plus `.meta.json`, not a
    swapped extension)."""
    document_path = Path(document_path)
    return document_path.with_name(document_path.name + settings.SIDECAR_SUFFIX)


def load_sidecar(document_path: Path) -> Mapping[str, Any]:
    """Load and validate the sidecar for one document.

    Raises `MissingSidecarError` if the file is absent, `InvalidSidecarError`
    if it exists but cannot be trusted. Returns the parsed metadata only when
    both the shape and the classification marking are sound.
    """
    document_path = Path(document_path)
    meta_path = sidecar_path(document_path)

    if not meta_path.is_file():
        raise MissingSidecarError(document_path)

    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidSidecarError(meta_path, f"could not be read: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidSidecarError(meta_path, f"not valid JSON: {exc}") from exc

    if not isinstance(data, Mapping):
        raise InvalidSidecarError(meta_path, "top-level JSON value is not an object")

    missing = sorted(REQUIRED_FIELDS - data.keys())
    if missing:
        raise InvalidSidecarError(
            meta_path, f"missing required field(s): {missing}"
        )

    acl = data.get("acl")
    if not isinstance(acl, list) or not acl or not all(isinstance(a, str) for a in acl):
        raise InvalidSidecarError(meta_path, "'acl' must be a non-empty list of strings")

    for field in ("classification", "department", "document_id", "document_version"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise InvalidSidecarError(meta_path, f"'{field}' must be a non-empty string")

    # Fail closed on a marking the lattice does not recognise (mirrors the
    # Policy Engine's own `UNKNOWN_CLASSIFICATION` rule, app/policy/engine.py)
    # rather than accepting an arbitrary string as a classification.
    try:
        Classification.rank(data["classification"])
    except ValueError as exc:
        raise InvalidSidecarError(meta_path, str(exc)) from exc

    return data
