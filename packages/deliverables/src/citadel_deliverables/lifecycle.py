"""Generating, storing, verifying, approving and releasing an artifact.

Status follows `citadel_contracts.state_machines.ARTIFACT` exactly -- TEMP ->
CANDIDATE -> VERIFIED -> APPROVED -> RELEASED -- and every transition goes through
`ARTIFACT.assert_transition`, so an illegal move fails in Python before it could reach
the database (where migration 0008 separately freezes a RELEASED row).

Approval is attached to release as an option, not a gate on every task: a deliverable
template with an approval block asks for it; one without is final once VERIFIED.
Acceptance re-renders the file from the exact inputs of the verified draft with the
approver's name and date in the approval block, re-verifies it, then freezes it: the
released bytes' SHA-256, the draft's SHA-256, and the whole provenance -- who asked,
what was retrieved, which model reasoned, what was computed, who approved -- become one
immutable record.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User
from citadel_contracts.state_machines import ARTIFACT, ArtifactStatus
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database, Json
from citadel_platform.registry.schema import TemplateEntry
from citadel_platform.storage import DataDir, safe_filename

from citadel_deliverables.content import Content, normalise
from citadel_deliverables.render import SourceRef, render_docx, render_xlsx, rendered_text
from citadel_deliverables.verify import Verification, verify

ORG_NAME_VAR = "CITADEL_ORG_NAME"
MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "py": "text/x-python",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "md": "text/markdown",
    "png": "image/png",
}


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskFacts:
    task_id: str
    classification: str  # uppercase lattice level
    goal: str


@dataclass
class Generated:
    artifact_id: str
    status: str
    filename: str
    verification: Verification
    sources: list[SourceRef]
    sha256: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "status": self.status,
            "filename": self.filename,
            "sha256": self.sha256,
            "version": self.version,
            "verification": self.verification.to_dict(),
            "sources": [s.to_dict() for s in self.sources],
        }


def _org_name() -> str:
    return os.environ.get(ORG_NAME_VAR) or "Citadel deployment"


def _record(audit: Optional[AuditLog], event: str, actor: Optional[str], payload: Mapping[str, Any]) -> None:
    if audit is not None:
        audit.record(event, actor_id=actor, payload=payload)


@lru_cache(maxsize=32)
def _template_markings(path: str, fmt: str, mtime: float) -> int:
    """How many times the template itself places the classification marking."""
    return rendered_text(fmt, Path(path).read_bytes()).count("{{classification}}")


def template_marking_count(registry_dir: Path, template: TemplateEntry) -> int:
    path = registry_dir / template.file
    return _template_markings(str(path), template.format, path.stat().st_mtime)


def cited_ids(template: TemplateEntry, raw_content: Mapping[str, Any]) -> list[str]:
    """Every evidence id the content cites, in order of first use -- what a caller must
    resolve (and nothing more) before calling `generate`."""
    return normalise(template, raw_content).all_citations()


def _sources(content: Content, evidence: Mapping[str, Mapping[str, Any]]) -> list[SourceRef]:
    refs: list[SourceRef] = []
    for number, cite in enumerate(content.all_citations(), start=1):
        row = evidence.get(cite)
        if row is None:
            refs.append(SourceRef(number, cite, "unresolved", f"UNRESOLVED CITATION {cite}", None, None, None))
            continue
        kind = str(row.get("kind") or "document")
        detail = row.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        if kind == "computation":
            title = f"Computation {cite}: {detail.get('expression', '')} = {detail.get('result', '')}"
            refs.append(SourceRef(number, cite, kind, title, None, None, None))
            continue
        bbox = row.get("bbox")
        refs.append(
            SourceRef(
                number,
                cite,
                kind,
                str(row.get("title") or detail.get("title") or row.get("document_id")),
                int(row["version"]) if row.get("version") is not None else None,
                int(row["page"]) if row.get("page") is not None else None,
                [float(v) for v in bbox] if bbox else None,
            )
        )
    return refs


def _fields(
    template: TemplateEntry,
    content: Content,
    task: TaskFacts,
    author: User,
    version: int,
) -> dict[str, str]:
    subject = next(
        (i.text for key in ("subject", "title", "equipment_tag") if key in content.sections
         for i in content.sections[key].items),
        "",
    )
    short = task.task_id.split("-")[0].upper()
    prefix = "".join(part[0] for part in template.id.split("-")).upper()
    today = date.today().isoformat()
    return {
        "org_name": _org_name(),
        "classification": task.classification,
        "doc_number": f"{prefix}-{short}",
        "revision": str(version - 1),
        "date": today,
        "prepared_by": f"{author.username} (via Citadel)",
        "prepared_signature": "Generated by Citadel - not signed",
        "task_ref": short,
        "subject_line": (subject or task.goal)[:160],
        "equipment": (subject or "-")[:120],
        "reviewed_by": "Verification ladder (automated)",
        "reviewed_signature": "Tiers 1-4 recorded",
        "reviewed_on": today,
        "approved_by": "PENDING APPROVAL" if template.approval_block else "Not required",
        "approved_signature": "-",
        "approved_on": "-",
    }


def _render(
    registry_dir: Path,
    template: TemplateEntry,
    content: Content,
    fields: Mapping[str, str],
    sources: Sequence[SourceRef],
    revisions: Sequence[Sequence[str]],
) -> bytes:
    path = registry_dir / template.file
    if template.format == "docx":
        return render_docx(path, content, fields=fields, sources=sources, revisions=revisions)
    if template.format == "xlsx":
        return render_xlsx(path, content, fields=fields, sources=sources)
    raise ArtifactError(f"template {template.id}: format {template.format!r} has no generator")


def _verify(
    registry_dir: Path,
    template: TemplateEntry,
    content: Content,
    data: bytes,
    task: TaskFacts,
    evidence: Mapping[str, Mapping[str, Any]],
    db: Optional[Database],
) -> Verification:
    return verify(
        template,
        content,
        rendered_text=rendered_text(template.format, data),
        marking=task.classification,
        evidence=evidence,
        db=db,
        min_markings=template_marking_count(registry_dir, template),
    )


def transition(db: Database, artifact_id: str, to_status: str) -> None:
    current = db.scalar("SELECT status FROM artifacts WHERE id = %(id)s::uuid", {"id": artifact_id})
    if current is None:
        raise ArtifactError(f"no artifact {artifact_id}")
    ARTIFACT.assert_transition(str(current), to_status)
    db.execute("UPDATE artifacts SET status = %(s)s WHERE id = %(id)s::uuid", {"s": to_status, "id": artifact_id})


def store_artifact(
    db: Database,
    data_dir: DataDir,
    *,
    task_id: str,
    kind: str,
    title: str,
    filename: str,
    data: bytes,
    classification: str,
    created_by: str,
    template_id: Optional[str] = None,
    requires_approval: bool = False,
    verification: Optional[Mapping[str, Any]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
    version: Optional[int] = None,
) -> tuple[str, str]:
    """Write the bytes and a TEMP row; returns (artifact_id, sha256 hex)."""
    Classification.rank(classification.upper())  # fail closed on an unknown marking
    artifact_id = str(uuid.uuid4())
    directory = data_dir.artifact_dir(artifact_id)
    path = directory / safe_filename(filename)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if version is None:
        version = int(db.scalar(
            "SELECT count(*) + 1 FROM artifacts WHERE task_id = %(t)s::uuid AND coalesce(template_id, filename) = %(k)s",
            {"t": task_id, "k": template_id or path.name},
        ) or 1)
    db.execute(
        "INSERT INTO artifacts (id, task_id, kind, classification, storage_ref, sha256, title, template_id, filename, "
        "mime_type, status, version, requires_approval, verification, provenance, created_by) VALUES "
        "(%(id)s::uuid, %(task)s::uuid, %(kind)s, %(c)s, %(ref)s, decode(%(sha)s, 'hex'), %(title)s, %(tpl)s, "
        "%(fn)s, %(mime)s, 'TEMP', %(v)s, %(approval)s, %(verification)s, %(provenance)s, %(by)s)",
        {
            "id": artifact_id, "task": task_id, "kind": kind, "c": classification.lower(),
            "ref": data_dir.relative(path), "sha": digest, "title": title[:300], "tpl": template_id,
            "fn": path.name, "mime": MIME.get(kind, "application/octet-stream"), "v": version,
            "approval": requires_approval, "verification": Json(dict(verification or {})),
            "provenance": Json(dict(provenance or {})), "by": created_by,
        },
    )
    return artifact_id, digest


def _prior_revisions(db: Database, task_id: str, template_id: str) -> list[list[str]]:
    rows = db.query(
        "SELECT a.version, a.created_at, coalesce(u.display_name, a.created_by) AS created_by, "
        "a.provenance -> 'render_inputs' ->> 'revision_note' AS note, "
        "(SELECT p.reason FROM approvals p WHERE p.artifact_id = a.id AND p.decision = 'rejected' LIMIT 1) AS rejection "
        "FROM artifacts a LEFT JOIN users u ON u.external_identity = a.created_by "
        "WHERE a.task_id = %(t)s::uuid AND a.template_id = %(k)s ORDER BY a.version",
        {"t": task_id, "k": template_id},
    )
    history: list[list[str]] = []
    for row in rows:
        description = str(row.get("note") or "Draft")
        if row.get("rejection") is not None:
            description += f"; rejected at review: {str(row['rejection'])[:120] or 'no comment'}"
        history.append([str(int(row["version"]) - 1), str(row["created_at"])[:10], description, str(row.get("created_by") or "")])
    return history


def generate(
    db: Database,
    data_dir: DataDir,
    registry_dir: Path,
    template: TemplateEntry,
    raw_content: Mapping[str, Any],
    *,
    task: TaskFacts,
    author: User,
    evidence: Mapping[str, Mapping[str, Any]],
    audit: Optional[AuditLog] = None,
    revision_note: Optional[str] = None,
) -> Generated:
    """Normalise, render into the fixed template, verify all four tiers, store. An
    artifact that passes tiers 1-3 advances TEMP -> CANDIDATE -> VERIFIED; one that
    does not stays TEMP, with its per-tier report, for the agent to revise."""
    Classification.rank(task.classification)  # fail closed on an unknown marking
    content = normalise(template, raw_content)
    sources = _sources(content, evidence)
    version = int(db.scalar(
        "SELECT count(*) + 1 FROM artifacts WHERE task_id = %(t)s::uuid AND template_id = %(k)s",
        {"t": task.task_id, "k": template.id},
    ) or 1)
    note = revision_note or ("Draft generated from the task's retrieved evidence" if version == 1 else "Revised draft")
    revisions = _prior_revisions(db, task.task_id, template.id)
    revisions.append([str(version - 1), date.today().isoformat(), note, author.username])
    fields = _fields(template, content, task, author, version)
    data = _render(registry_dir, template, content, fields, sources, revisions)
    verification = _verify(registry_dir, template, content, data, task, evidence, db)
    filename = f"{template.id}-{task.task_id.split('-')[0]}-v{version}.{template.format}"
    digest = hashlib.sha256(data).hexdigest()
    label, subject = template.id.replace("-", " ").capitalize(), fields["subject_line"] or task.goal[:80]
    artifact_id, _ = store_artifact(
        db,
        data_dir,
        task_id=task.task_id,
        kind=template.format,
        # "Report: Report on lathe L-1" reads twice; a subject that already names the kind stands alone.
        title=subject if subject.lower().startswith(label.lower()) else f"{label}: {subject}",
        filename=filename,
        data=data,
        classification=task.classification,
        created_by=author.user_id,
        template_id=template.id,
        requires_approval=template.approval_block,
        verification=verification.to_dict(),
        provenance={
            "render_inputs": {
                "content": dict(raw_content),
                "fields": fields,
                "revisions": revisions,
                "revision_note": note,
                "author": {"user_id": author.user_id, "username": author.username},
            },
            "sources": [s.to_dict() for s in sources],
            "draft_sha256": digest,
        },
        version=version,
    )
    _record(audit, "artifact.generated", author.user_id, {
        "artifact_id": artifact_id, "task_id": task.task_id, "template_id": template.id, "version": version, "sha256": digest,
    })
    status = ArtifactStatus.TEMP
    if verification.passed:
        transition(db, artifact_id, ArtifactStatus.CANDIDATE)
        transition(db, artifact_id, ArtifactStatus.VERIFIED)
        status = ArtifactStatus.VERIFIED
        _record(audit, "artifact.verified", author.user_id, {
            "artifact_id": artifact_id, "task_id": task.task_id, "summary": verification.summary(),
            "flagged_claims": len(verification.flagged_claims),
        })
        if template.approval_block:
            _record(audit, "approval.requested", author.user_id, {"artifact_id": artifact_id, "task_id": task.task_id})
    else:
        failed = [t.to_dict() for t in verification.tiers if t.status == "fail"]
        _record(audit, "artifact.failed_tier", author.user_id, {
            "artifact_id": artifact_id, "task_id": task.task_id, "failed": failed,
        })
    return Generated(artifact_id, status, filename, verification, sources, digest, version)


def get_artifact(db: Database, artifact_id: str) -> Optional[dict[str, Any]]:
    return db.query_one(
        "SELECT a.id::text AS id, a.task_id::text AS task_id, a.kind, a.classification, a.storage_ref, "
        "encode(a.sha256, 'hex') AS sha256, a.title, a.template_id, a.filename, a.mime_type, a.status, a.version, "
        "a.requires_approval, a.verification, a.provenance, a.created_by, a.created_at, a.released_at, "
        "t.goal, t.status AS task_status, u.external_identity AS task_owner, u.department AS owner_department "
        "FROM artifacts a JOIN tasks t ON t.id = a.task_id JOIN users u ON u.id = t.submitted_by "
        "WHERE a.id = %(id)s::uuid",
        {"id": artifact_id},
    )


def read_bytes(db: Database, data_dir: DataDir, artifact_id: str) -> tuple[dict[str, Any], bytes]:
    """The stored bytes, checked against the recorded hash -- the column is the claim,
    this read is the proof (migration 0003's comment on artifacts.sha256)."""
    artifact = get_artifact(db, artifact_id)
    if artifact is None:
        raise ArtifactError("no such artifact")
    data = data_dir.resolve(str(artifact["storage_ref"])).read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ArtifactError(f"artifact {artifact_id} bytes do not match the recorded SHA-256")
    return artifact, data


def decide(
    db: Database,
    data_dir: DataDir,
    registry_dir: Path,
    templates: Sequence[TemplateEntry],
    *,
    artifact_id: str,
    approver: User,
    approve: bool,
    comment: str,
    audit: Optional[AuditLog] = None,
) -> dict[str, Any]:
    """Record an approver's decision. Acceptance re-renders with the approval block
    filled, re-verifies, freezes the bytes and releases; rejection leaves the artifact
    VERIFIED and tells the caller the task has its one bounded revision."""
    artifact = get_artifact(db, artifact_id)
    if artifact is None:
        raise ArtifactError("no such artifact")
    if artifact["status"] != ArtifactStatus.VERIFIED or not artifact["requires_approval"]:
        raise ArtifactError(f"artifact is {artifact['status']}; only a VERIFIED artifact awaiting approval can be decided")
    if approver.user_id == artifact["task_owner"]:
        raise ArtifactError("the person who asked for a deliverable cannot approve it")
    if db.scalar("SELECT count(*) FROM approvals WHERE artifact_id = %(a)s::uuid", {"a": artifact_id}):
        raise ArtifactError("this artifact already has a decision")
    newer = db.scalar(
        "SELECT max(version) FROM artifacts WHERE task_id = %(t)s::uuid AND template_id = %(k)s AND version > %(v)s",
        {"t": artifact["task_id"], "k": artifact["template_id"], "v": int(artifact["version"])},
    )
    if newer:
        raise ArtifactError(f"a newer version (v{newer}) of this deliverable exists; decide on that one")
    now = datetime.now(timezone.utc)
    approval_row = (
        "INSERT INTO approvals (artifact_id, approver_id, decision, reason, decided_at) VALUES "
        "(%(a)s::uuid, (SELECT id FROM users WHERE external_identity = %(u)s), %(d)s, %(r)s, %(t)s)"
    )

    if not approve:
        db.execute(approval_row, {"a": artifact_id, "u": approver.user_id, "d": "rejected", "r": comment[:2000], "t": now})
        _record(audit, "approval.rejected", approver.user_id, {
            "artifact_id": artifact_id, "task_id": artifact["task_id"], "comment": comment[:500],
        })
        return {"decision": "rejected", "artifact_id": artifact_id, "status": artifact["status"], "task_id": artifact["task_id"]}

    provenance = dict(artifact["provenance"] or {})
    inputs = provenance.get("render_inputs") or {}
    template = next((t for t in templates if t.id == artifact["template_id"]), None)
    if template is None or not inputs:
        raise ArtifactError("the verified draft's render inputs are missing; it cannot be re-rendered for release")
    _, draft = read_bytes(db, data_dir, artifact_id)
    content = normalise(template, inputs.get("content") or {})
    sources = [SourceRef(**{**s, "bbox": s.get("bbox")}) for s in provenance.get("sources") or []]
    fields = dict(inputs.get("fields") or {})
    fields.update({
        "approved_by": f"{approver.username} ({approver.department})" if approver.department else approver.username,
        "approved_signature": "Approved in Citadel",
        "approved_on": now.date().isoformat(),
    })
    revisions = [list(r) for r in inputs.get("revisions") or []]
    revisions.append([fields.get("revision", "0"), now.date().isoformat(), f"Approved for release: {comment[:100] or 'no comment'}", approver.username])
    data = _render(registry_dir, template, content, fields, sources, revisions)
    task = TaskFacts(str(artifact["task_id"]), str(artifact["classification"]).upper(), str(artifact["goal"]))
    evidence = {
        str(r["evidence_id"]): r
        for r in db.query(
            "SELECT e.evidence_id, e.kind, e.document_id::text AS document_id, e.version, e.page, e.bbox, e.text, "
            "e.classification, e.detail FROM task_evidence e WHERE e.task_id = %(t)s::uuid",
            {"t": task.task_id},
        )
    }
    release_check = _verify(registry_dir, template, content, data, task, evidence, db)
    if not release_check.passed:
        raise ArtifactError(f"the release rendering failed verification: {release_check.summary()}")

    db.execute(approval_row, {"a": artifact_id, "u": approver.user_id, "d": "approved", "r": comment[:2000], "t": now})
    _record(audit, "approval.accepted", approver.user_id, {
        "artifact_id": artifact_id, "task_id": artifact["task_id"], "comment": comment[:500],
    })
    transition(db, artifact_id, ArtifactStatus.APPROVED)
    data_dir.resolve(str(artifact["storage_ref"])).write_bytes(data)
    released_sha = hashlib.sha256(data).hexdigest()
    provenance.update({
        "draft_sha256": provenance.get("draft_sha256") or hashlib.sha256(draft).hexdigest(),
        "released_sha256": released_sha,
        "release_verification": release_check.to_dict(),
        "approval": {
            "approver": approver.user_id, "name": approver.username, "department": approver.department,
            "comment": comment, "at": now.isoformat(),
        },
    })
    ARTIFACT.assert_transition(ArtifactStatus.APPROVED, ArtifactStatus.RELEASED)
    db.execute(
        "UPDATE artifacts SET sha256 = decode(%(sha)s, 'hex'), provenance = %(p)s WHERE id = %(id)s::uuid",
        {"sha": released_sha, "p": Json(provenance), "id": artifact_id},
    )
    provenance["record"] = build_provenance(db, artifact_id, extra=provenance)
    db.execute(
        "UPDATE artifacts SET status = 'RELEASED', released_at = %(t)s, provenance = %(p)s WHERE id = %(id)s::uuid",
        {"t": now, "p": Json(provenance), "id": artifact_id},
    )
    _record(audit, "artifact.released", approver.user_id, {
        "artifact_id": artifact_id, "task_id": artifact["task_id"], "sha256": released_sha,
        "draft_sha256": provenance["draft_sha256"],
    })
    return {
        "decision": "approved", "artifact_id": artifact_id, "status": ArtifactStatus.RELEASED,
        "sha256": released_sha, "task_id": artifact["task_id"],
    }


def build_provenance(db: Database, artifact_id: str, *, extra: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The whole chain, as one record: who asked, what was retrieved, which model
    reasoned and why, what was computed, who approved, what left the building."""
    artifact = get_artifact(db, artifact_id)
    if artifact is None:
        return {}
    task_id = str(artifact["task_id"])
    task = db.query_one(
        "SELECT t.id::text AS id, t.goal, t.classification, t.status, t.created_at, t.primary_capability, "
        "u.external_identity AS submitted_by, u.display_name, u.department FROM tasks t JOIN users u ON u.id = t.submitted_by "
        "WHERE t.id = %(t)s::uuid",
        {"t": task_id},
    )
    models = db.query(
        "SELECT attributes ->> 'model_id' AS model_id, attributes ->> 'purpose' AS purpose, "
        "attributes -> 'routing' ->> 'reason' AS reason, attributes -> 'usage' AS usage, started_at "
        "FROM trace_spans WHERE task_id = %(t)s::uuid AND kind = 'model' AND attributes ? 'model_id' ORDER BY started_at",
        {"t": task_id},
    )
    evidence_rows = db.query(
        "SELECT e.evidence_id, e.kind, e.document_id::text AS document_id, d.title, e.version, e.page, e.bbox, "
        "e.detail FROM task_evidence e LEFT JOIN documents d ON d.id = e.document_id "
        "WHERE e.task_id = %(t)s::uuid ORDER BY e.created_at, e.evidence_id",
        {"t": task_id},
    )
    approvals = db.query(
        "SELECT a.decision, a.reason, a.decided_at, u.external_identity AS approver FROM approvals a "
        "JOIN users u ON u.id = a.approver_id WHERE a.artifact_id = %(a)s::uuid ORDER BY a.decided_at",
        {"a": artifact_id},
    )
    egress = db.query_one(
        "SELECT count(*) FILTER (WHERE kind = 'attempt') AS attempts, count(*) FILTER (WHERE kind = 'blocked') AS blocked, "
        "count(*) FILTER (WHERE kind = 'dns_denied') AS dns_denied FROM egress_events WHERE task_id = %(t)s::uuid",
        {"t": task_id},
    )
    record: dict[str, Any] = {
        "artifact": {k: artifact[k] for k in ("id", "title", "template_id", "filename", "classification", "version", "sha256")},
        "task": task,
        "models": models,
        "evidence": evidence_rows,
        "verification": artifact["verification"],
        "approvals": approvals,
        "sovereignty": egress,
    }
    if extra:
        record["release"] = {k: extra[k] for k in ("released_sha256", "draft_sha256", "approval") if k in extra}
    return record


__all__ = [
    "ArtifactError",
    "TaskFacts",
    "Generated",
    "MIME",
    "ORG_NAME_VAR",
    "transition",
    "store_artifact",
    "generate",
    "get_artifact",
    "read_bytes",
    "decide",
    "build_provenance",
    "cited_ids",
    "template_marking_count",
]
