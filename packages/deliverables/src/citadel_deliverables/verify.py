"""The four-tier verification ladder (packages/deliverables/AGENTS.md).

Run before anything is shown as final, and reported per tier, so a failure says which
tier failed and not merely that something did:

1. **Structural** -- the template's declared sections are present and populated, the
   rendered file carries every declared marking, and no placeholder survived. Driven by
   the template's declaration in registry/templates.yaml, never by hardcoded headings
   (the prototype checked for three literal strings and so could verify exactly one
   kind of document).
2. **Schema** -- every section has the declared type and minimum size.
3. **Citation** -- every cited section item carries a citation, and every citation
   resolves to evidence the task was actually given: a real document, version, page,
   and a well-formed region.
4. **Grounding** -- every quantitative claim in a cited section traces to a number in
   the evidence *that item* cites, within tolerance. An untraceable number is
   surfaced -- `flagged`, with the number and the sentence -- never silently dropped
   and never silently passed. Tiers 1-3 must pass for an artifact to be VERIFIED; a
   tier-4 flag travels with it to the approver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_platform.db import Database
from citadel_platform.registry.schema import TemplateEntry

from citadel_deliverables.content import Content

_IDENTIFIER = re.compile(r"\b[A-Za-z]{1,6}(?:-[A-Za-z0-9]+)*-\d[\w./-]*")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATES = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"                                   # 2026-03-14
    r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"                     # 14/03/2026
    rf"|\b\d{{1,2}}\s+(?:{_MONTHS})[a-z]*\.?,?\s+\d{{4}}\b"      # 14 March 2026
    rf"|\b(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}}\b",     # March 14, 2026
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.]*\d)")


def quantities(text: str) -> list[str]:
    """Numbers a reader would take as quantitative claims -- not the digits inside
    identifiers like E-101, CML-3 or IR-2026-0147, and not calendar dates, which name
    a time rather than state a quantity."""
    stripped = _DATES.sub(" ", _IDENTIFIER.sub(" ", text))
    return _NUMBER.findall(stripped)


def _decimals(number: str) -> int:
    return len(number.split(".")[1]) if "." in number else 0


def is_grounded(claim: str, source_numbers: Sequence[str], relative_tolerance: float = 0.0) -> bool:
    value = float(claim)
    rounding = 0.5 * 10 ** (-_decimals(claim))
    for candidate in source_numbers:
        other = float(candidate)
        if abs(value - other) <= max(relative_tolerance * abs(other), rounding if _decimals(claim) < _decimals(candidate) else 0.0) + 1e-12:
            return True
    return False


@dataclass
class TierResult:
    tier: int
    name: str
    status: str  # pass | fail | flagged
    issues: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "name": self.name, "status": self.status, "issues": self.issues, "details": self.details}


@dataclass
class Verification:
    tiers: list[TierResult]

    @property
    def passed(self) -> bool:
        return all(t.status == "pass" for t in self.tiers if t.tier <= 3)

    @property
    def flagged_claims(self) -> list[dict[str, Any]]:
        for tier in self.tiers:
            if tier.tier == 4:
                claims = tier.details.get("ungrounded") or []
                return list(claims)
        return []

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "tiers": [t.to_dict() for t in self.tiers],
            "flagged_claims": self.flagged_claims,
        }

    def summary(self) -> str:
        return ", ".join(f"tier {t.tier} {t.name}: {t.status}" for t in self.tiers)


def _tier_structural(
    template: TemplateEntry, content: Content, rendered_text: str, marking: str, min_markings: int
) -> TierResult:
    issues: list[str] = []
    for spec in template.sections:
        section = content.sections[spec.key]
        if spec.required and section.empty:
            issues.append(f"required section '{spec.key}' is missing or empty")
    if "{{" in rendered_text:
        leftovers = sorted(set(re.findall(r"\{\{[^}]*\}\}", rendered_text)))
        issues.append(f"placeholders left unfilled in the rendered file: {', '.join(leftovers)}")
    for spec in template.sections:
        section = content.sections[spec.key]
        if not section.empty:
            sample = next(((i.heading if spec.type == "sections" else i.text) for i in section.items if i.text.strip()), "")
            probe = " ".join(sample.split()[:6]).rstrip(".;:,")  # markers may sit before a final stop
            if probe and probe not in " ".join(rendered_text.split()):
                issues.append(f"section '{spec.key}' content does not appear in the rendered file")
    if template.classification_markings:
        carried = rendered_text.count(marking)
        if carried < max(1, min_markings):
            issues.append(
                f"classification marking {marking} is carried {carried} time(s); the template places it {min_markings} time(s)"
            )
    if template.approval_block and "Approved by" not in rendered_text:
        issues.append("the template declares an approval block, and the rendered file has none")
    if template.revision_history and "Revision history" not in rendered_text:
        issues.append("the template declares a revision history, and the rendered file has none")
    return TierResult(1, "structural", "fail" if issues else "pass", issues)


def _tier_schema(content: Content) -> TierResult:
    return TierResult(2, "schema", "fail" if content.issues else "pass", list(content.issues))


def _tier_citation(
    template: TemplateEntry,
    content: Content,
    evidence: Mapping[str, Mapping[str, Any]],
    db: Optional[Database],
    marking: str,
) -> TierResult:
    issues: list[str] = []
    for spec in template.sections:
        if not spec.cited:
            continue
        section = content.sections[spec.key]
        if section.empty:
            continue
        if spec.type in ("list", "table", "sections"):
            for index, item in enumerate(section.items, start=1):
                if (item.text.strip() or item.cells) and not item.citations:
                    named = f" ('{item.heading}')" if item.heading else ""
                    issues.append(f"section '{spec.key}' item {index}{named} carries no citation")
        elif not section.citations:
            issues.append(f"section '{spec.key}' carries no citation")
    checked = 0
    for cite in content.all_citations():
        row = evidence.get(cite)
        if row is None:
            issues.append(f"citation {cite} does not resolve to evidence this task was given")
            continue
        checked += 1
        level = str(row.get("classification") or "").upper()
        if level:
            try:
                above = Classification.exceeds(level, marking)
            except ValueError:
                above = True
            if above:
                issues.append(f"citation {cite} is {level} evidence in a document marked {marking}")
        if row.get("kind") == "document":
            bbox = row.get("bbox")
            if bbox is not None:
                if len(bbox) != 4 or not (0 <= bbox[0] < bbox[2] <= 1 and 0 <= bbox[1] < bbox[3] <= 1):
                    issues.append(f"citation {cite} has a malformed region {bbox}")
            if db is not None:
                exists = db.scalar(
                    "SELECT count(*) FROM document_pages WHERE document_id = %(d)s::uuid AND version = %(v)s AND page = %(p)s",
                    {"d": str(row.get("document_id")), "v": int(row.get("version") or 0), "p": int(row.get("page") or 0)},
                )
                if not exists:
                    issues.append(f"citation {cite} points at a page that does not exist")
    return TierResult(3, "citation", "fail" if issues else "pass", issues, {"citations_checked": checked})


def _grounding_units(section: Any) -> list[tuple[int, Any, list[str], str]]:
    """What each quantitative claim is checked against: an item's own citations -- or,
    in a report body, each paragraph's own, falling back to its section's when the
    paragraph cites nothing itself."""
    units: list[tuple[int, Any, list[str], str]] = []
    for index, item in enumerate(section.items, start=1):
        if item.parts:
            for part in item.parts:
                units.append((index, item, part.citations or item.citations, part.text))
        else:
            units.append((index, item, item.citations, " ".join([item.text, *item.cells.values()])))
    return units


def _tier_grounding(template: TemplateEntry, content: Content, evidence: Mapping[str, Mapping[str, Any]]) -> TierResult:
    tolerance = template.grounding.derived_value_tolerance or 0.0
    ungrounded: list[dict[str, Any]] = []
    traced = 0
    for spec in template.sections:
        if not spec.cited:
            continue
        for index, item, cites, text in _grounding_units(content.sections[spec.key]):
            claims = quantities(text)
            if not claims:
                continue
            source_numbers: list[str] = []
            for cite in cites:
                row = evidence.get(cite)
                if row is not None:
                    source_numbers.extend(quantities(str(row.get("text") or "")))
                    detail = row.get("detail") or {}
                    if isinstance(detail, Mapping) and "result" in detail:
                        source_numbers.extend(quantities(str(detail["result"])))
            for claim in claims:
                if is_grounded(claim, source_numbers, tolerance):
                    traced += 1
                else:
                    ungrounded.append({"section": spec.key, "item": index, "number": claim, "text": text[:240]})
    status = "flagged" if ungrounded else "pass"
    issues = [f"{c['number']} in '{c['section']}' item {c['item']} is not found in the evidence it cites" for c in ungrounded]
    return TierResult(4, "grounding", status, issues, {"traced": traced, "ungrounded": ungrounded})


def verify(
    template: TemplateEntry,
    content: Content,
    *,
    rendered_text: str,
    marking: str,
    evidence: Mapping[str, Mapping[str, Any]],
    db: Optional[Database] = None,
    min_markings: int = 1,
) -> Verification:
    """`min_markings` is how many times the template itself places the classification
    marking (header, footer, banner...); the rendered file must carry at least as many."""
    return Verification(
        [
            _tier_structural(template, content, rendered_text, marking, min_markings),
            _tier_schema(content),
            _tier_citation(template, content, evidence, db, marking),
            _tier_grounding(template, content, evidence),
        ]
    )


__all__ = ["TierResult", "Verification", "verify", "quantities", "is_grounded"]
