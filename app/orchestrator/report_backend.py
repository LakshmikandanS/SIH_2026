"""THE `generate_report` Tool Gateway backend -- a deliberately minimal seam.

    "generate_report is step 8's deliverable (template, Verifier, approval
     endpoint). You must NOT build the report template, the five structural
     checks, or the approval decision endpoint. [...] register a
     deliberately minimal `generate_report` backend that writes a
     placeholder artifact and creates the Artifact row. Mark it clearly [...]
     Keep it small."

This is exactly that placeholder, nothing more:

  * NOT the real `maintenance_summary_v1` template (design doc section
    6.10's "exactly one template exists" -- that template is step 8's).
  * NOT the Verifier's five structural checks (section 6.10) -- this backend
    never inspects its own output for correctness; it only writes it.
  * NOT approval routing -- the artifact this creates stays at
    `ArtifactStatus.TEMP` (its schema-default initial state,
    `app/db/state_machines.py`); moving it to `CANDIDATE`/`VERIFIED` is
    step 8's Verifier, which does not run here.

Registered exactly like every other tool backend (`app/rag/backend.py`'s own
docstring is the reference pattern):

    from app.policy import Tool
    from app.tool_gateway import register_backend
    from app.orchestrator.report_backend import generate_report_backend

    register_backend(Tool.GENERATE_REPORT, generate_report_backend)

Following `app/tool_gateway/backends/echo.py`'s contract: this function only
returns the `result` body of the success envelope (a file path, a hash, the
list of evidence ids actually cited) -- it does not create the `Artifact`
row itself. `app.orchestrator.agent_loop` does that, with the DB write
credential the Orchestrator holds (design doc section 6.11: only the
Orchestrator commits authoritative state) -- keeping this backend as pure a
"render a file, report what happened" function as `python.execute`'s own
backend is.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from app import ids

#: `var/artifacts/<task_id>/<artifact_id>.md` -- next to `var/citadel.db`
#: (`app/config.py`'s `VAR_DIR`), not inside `data/` (the read-only RAG
#: corpus) and not inside the isolated execution zone.
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "var" / "artifacts"


def _render_markdown(
    *,
    template: str,
    evidence: list[Mapping[str, Any]],
    computed: Mapping[str, Any] | None,
    revision_comment: str | None,
) -> str:
    lines: list[str] = [
        f"# Maintenance Summary Report ({template})",
        "",
        "> Placeholder artifact -- the real template, the Verifier's five",
        "> structural checks, and approval routing are step 8's",
        "> (`artifact-pipeline`) deliverable, not this seam's.",
        "",
        "## Summary",
        "",
    ]
    if computed:
        lines.append(
            f"Most recent service: {computed.get('most_recent')}. "
            f"Days since last service: {computed.get('days_since')}. "
            f"Service records found: {computed.get('records_found')}."
        )
    else:
        lines.append("No computed maintenance figures were available.")

    lines += ["", "## Maintenance History", ""]
    for row in evidence:
        text = str(row.get("text", "")).replace("\n", " ")[:200]
        lines.append(f"- ({row.get('document_id')} p{row.get('page')}) {text}")
    if not evidence:
        lines.append("- (no evidence was retrieved)")

    lines += ["", "## Sources", ""]
    for row in evidence:
        lines.append(f"- {row.get('evidence_id')} -> {row.get('document_id')}")
    if not evidence:
        lines.append("- (none)")

    if revision_comment:
        lines += ["", "## Revision Note", "", revision_comment]

    return "\n".join(lines) + "\n"


def generate_report_backend(request: Any) -> Mapping[str, Any]:
    """Render the placeholder report and return enough for the caller to
    build the `Artifact` row (`artifact_id`, `path`, `hash`, `provenance`).

    `request` is `app.tool_gateway.registry.ToolRequest`; typed as `Any` here
    only to avoid importing the Tool Gateway's registry module purely for a
    type hint (`app/tool_gateway/backends/echo.py` does the same).
    """
    task_id = request.capability.task_id
    evidence = list(request.arguments.get("evidence") or [])
    computed = request.arguments.get("computed")
    template = request.arguments.get("template") or "maintenance_summary_v1"
    revision_comment = request.arguments.get("revision_comment")

    body = _render_markdown(
        template=template, evidence=evidence, computed=computed, revision_comment=revision_comment
    )

    artifact_id = ids.new_id(ids.ARTIFACT)
    artifact_dir = ARTIFACTS_DIR / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{artifact_id}.md"
    path.write_text(body, encoding="utf-8")

    provenance = [row["evidence_id"] for row in evidence if "evidence_id" in row]
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    return {
        "artifact_id": artifact_id,
        "path": str(path),
        "hash": content_hash,
        "provenance": provenance,
        "template": template,
        "revised": bool(revision_comment),
    }


def register() -> None:
    """Attach this backend the same way every other tool does (section 6.6)."""
    from app.policy import Tool
    from app.tool_gateway import register_backend

    register_backend(Tool.GENERATE_REPORT, generate_report_backend)
