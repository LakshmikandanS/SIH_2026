"""The one template (design doc section 6.10, BB-045): `maintenance_summary_v1`.

    "Template selection (BB-045): exactly one template exists
     (`maintenance_summary_v1`); "selection" is a no-op."

There is no template registry, no lookup by name, no second template to
select between -- `render` below is the entire "template engine". Markdown
only (section 6.10: "Render Markdown (not DOCX/PDF -- that generality is
explicitly out of scope for this slice)"); the `## Summary` / `## Maintenance
History` / `## Sources` headers are exactly what
`app.artifact.verifier.has_required_sections` checks for, so the two must
never drift apart -- `REQUIRED_SECTIONS` (`app.artifact.verifier`) names the
section titles once and this module renders exactly those headers.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

#: The only template this slice knows. Not a dict of templates -- there is
#: nothing to select (BB-045).
TEMPLATE_NAME = "maintenance_summary_v1"


def render(
    *,
    evidence: list[Mapping[str, Any]],
    computed: Optional[Mapping[str, Any]],
    revision_comment: Optional[str] = None,
) -> str:
    """Render the fixed Markdown report from the evidence + computed figures
    THINK already gathered (`app.orchestrator.think._think_generate_report`).

    Section order matches design doc section 1.1 step 13 / section 6.10:
    Summary, Maintenance History, Sources. A revision (section 5.3) appends
    one more section carrying the approver's comment, so the regenerated
    report visibly responds to it.
    """
    lines: list[str] = [
        "# Maintenance Summary Report",
        "",
        f"_Template: {TEMPLATE_NAME}_",
        "",
        "## Summary",
        "",
    ]
    if computed:
        most_recent = computed.get("most_recent")
        days_since = computed.get("days_since")
        records_found = computed.get("records_found")
        lines.append(
            f"Most recent service: {most_recent}. "
            f"Days since last service: {days_since}. "
            f"Service records found: {records_found}."
        )
    else:
        lines.append("No computed maintenance figures were available.")

    lines += ["", "## Maintenance History", ""]
    for row in evidence:
        text = str(row.get("text", "")).replace("\n", " ").strip()[:200]
        document_id = row.get("document_id", "?")
        page = row.get("page", "?")
        lines.append(f"- ({document_id} p{page}) {text}")
    if not evidence:
        lines.append("- (no evidence was retrieved)")

    lines += ["", "## Sources", ""]
    for row in evidence:
        evidence_id = row.get("evidence_id", "?")
        document_id = row.get("document_id", "?")
        lines.append(f"- {evidence_id} -> {document_id}")
    if not evidence:
        lines.append("- (none)")

    if revision_comment:
        lines += ["", "## Revision Note", "", revision_comment]

    return "\n".join(lines) + "\n"
