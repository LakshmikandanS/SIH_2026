"""The Verifier (design doc section 6.10, BB-043) -- a plain function, not a
service, not an LLM judge:

    def verify(artifact) -> bool:
        checks = [
            artifact.file_exists_and_readable(),
            artifact.compute_and_store_sha256(),
            artifact.has_required_sections(["Summary", "Maintenance History", "Sources"]),
            all(e.classification <= task.classification for e in artifact.cited_evidence()),
            len(artifact.provenance) > 0,
        ]
        return all(checks)   # any failure => task FAILED for this slice, no auto-revision

Copied here as literally as the design doc's own pseudocode allows: five
checks, `all()` over them, no LLM call, no per-check retry. The one
substitution the mission brief itself requires is the fourth check --
`Classification.exceeds` (`app.db.state_machines`) replaces the `<=` string
comparison, because this codebase's own house rule (steps 4 and 6 both follow
it) is that classification is never compared as a plain string.

`VerifiableArtifact` is the adapter that gives a SQLAlchemy `Artifact` row the
five method names the pseudocode calls directly -- `verify()` itself never
touches `Artifact`/`Task` columns, only this adapter's interface, so the
function above really is "copy these five checks exactly" and nothing more.
`run_verification` is the thin wrapper the pipeline (`app.artifact.pipeline`)
and the demo actually call: same five checks, but returned as a named report
so the ARTIFACT_VERIFIED event payload and the demo's printed checklist don't
have to re-derive which of the five failed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.db.models import Artifact, Task
from app.db.state_machines import Classification

#: Design doc section 6.10's own three section titles, in this exact order.
#: `app.artifact.template.render` renders `## <title>` headers for each of
#: these -- the two modules must never drift apart, which is why this is the
#: one place the titles are spelled out and `render` does not repeat them.
REQUIRED_SECTIONS: tuple[str, ...] = ("Summary", "Maintenance History", "Sources")


@dataclass(frozen=True)
class CitedEvidence:
    """One evidence row the artifact actually cites -- just enough of the
    section 3 Evidence schema for the fourth check (`classification`)."""

    evidence_id: str
    classification: str


@dataclass
class VerifiableArtifact:
    """Adapter: a session-attached `Artifact` row (+ its `Task`, + the
    evidence rows `generate_report` was given) exposing exactly the five
    pseudocode method names. `artifact`/`task` are the live SQLAlchemy rows
    -- `compute_and_store_sha256` writes to `artifact.hash` directly, which
    is why this adapter (not a copy) is what `verify()`/`run_verification`
    must be called with.
    """

    artifact: Artifact
    task: Task
    evidence: Sequence[Mapping[str, Any]]

    def file_exists_and_readable(self) -> bool:
        if not self.artifact.path:
            return False
        try:
            path = Path(self.artifact.path)
            return path.is_file() and os.access(path, os.R_OK)
        except OSError:
            return False

    def compute_and_store_sha256(self) -> bool:
        """Hash the file *on disk* (never the in-memory render) -- this is
        the check that would actually catch a corrupted/truncated write."""
        if not self.artifact.path:
            return False
        try:
            data = Path(self.artifact.path).read_bytes()
        except OSError:
            return False
        self.artifact.hash = hashlib.sha256(data).hexdigest()
        return True

    def has_required_sections(self, sections: Sequence[str]) -> bool:
        if not self.artifact.path:
            return False
        try:
            text = Path(self.artifact.path).read_text(encoding="utf-8")
        except OSError:
            return False
        return all(f"## {section}" in text for section in sections)

    def cited_evidence(self) -> list[CitedEvidence]:
        """Evidence rows whose `evidence_id` is in the artifact's own
        `provenance` list -- section 6.9's "provenance IS the evidence row's
        own primary key" -- filtered down to what this artifact actually
        cites, not everything `generate_report` merely saw."""
        cited_ids = set(self.provenance)
        out: list[CitedEvidence] = []
        for row in self.evidence:
            evidence_id = row.get("evidence_id")
            if evidence_id in cited_ids:
                out.append(
                    CitedEvidence(
                        evidence_id=evidence_id,
                        classification=row.get("classification", ""),
                    )
                )
        return out

    @property
    def provenance(self) -> list[str]:
        return list(self.artifact.provenance or [])


@dataclass(frozen=True)
class VerificationReport:
    """The five checks, named, plus the sha256 the second check stored --
    what `app.observability`'s ARTIFACT_VERIFIED payload and the step 8 demo
    both print."""

    file_exists_and_readable: bool
    sha256_computed: bool
    has_required_sections: bool
    evidence_classification_ok: bool
    has_provenance: bool
    sha256: Optional[str]

    @property
    def passed(self) -> bool:
        return all(
            [
                self.file_exists_and_readable,
                self.sha256_computed,
                self.has_required_sections,
                self.evidence_classification_ok,
                self.has_provenance,
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_exists_and_readable": self.file_exists_and_readable,
            "sha256_computed": self.sha256_computed,
            "has_required_sections": self.has_required_sections,
            "evidence_classification_ok": self.evidence_classification_ok,
            "has_provenance": self.has_provenance,
            "sha256": self.sha256,
            "passed": self.passed,
        }


def run_verification(artifact: VerifiableArtifact) -> VerificationReport:
    """The five checks, run once, returned as a named report. `verify()`
    below is `run_verification(artifact).passed` -- the pseudocode's literal
    `all(checks)` shape, not reimplemented, just given a name for each
    element so a failure is diagnosable rather than a single opaque bool.
    """
    task = artifact.task

    file_ok = artifact.file_exists_and_readable()
    hash_ok = artifact.compute_and_store_sha256()
    sections_ok = artifact.has_required_sections(list(REQUIRED_SECTIONS))
    # `Classification.exceeds`, never `<=` on the raw strings (house rule --
    # steps 4 and 6 both follow it, see this module's docstring).
    classification_ok = all(
        not Classification.exceeds(e.classification, task.classification)
        for e in artifact.cited_evidence()
    )
    provenance_ok = len(artifact.provenance) > 0

    return VerificationReport(
        file_exists_and_readable=file_ok,
        sha256_computed=hash_ok,
        has_required_sections=sections_ok,
        evidence_classification_ok=classification_ok,
        has_provenance=provenance_ok,
        sha256=artifact.artifact.hash,
    )


def verify(artifact: VerifiableArtifact) -> bool:
    """design doc section 6.10's `verify(artifact) -> bool`, exactly:
    `all()` over the five checks, no LLM judge, no auto-revision on failure
    (BB-038 -- a failure is the caller's job to turn into a FAILED task)."""
    return run_verification(artifact).passed
