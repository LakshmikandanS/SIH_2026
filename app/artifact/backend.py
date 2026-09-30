"""THE real `generate_report` Tool Gateway backend (design doc section 6.10),
replacing `app.orchestrator.report_backend`'s deliberately minimal seam.

    "generate_report renders the one fixed Markdown/DOCX template with the
     evidence + computed fields." (section 1.1 step 13)

Following `app/tool_gateway/backends/echo.py`'s (and the seam's own) contract:
this function only returns the `result` body of the success envelope -- it
does not create the `Artifact` row itself, and it does not run the Verifier.
Both of those are `app.artifact.pipeline.create_and_verify_artifact`'s job,
called from `app.orchestrator.agent_loop._commit_artifact` right after this
backend returns (design doc section 6.11: only the Orchestrator commits
authoritative state). Keeping this backend a pure "render a file, report what
happened" function mirrors `python.execute`'s own backend.

Registered exactly like every other tool backend:

    from app.policy import Tool
    from app.tool_gateway import register_backend
    from app.artifact.backend import generate_report_backend

    register_backend(Tool.GENERATE_REPORT, generate_report_backend)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app import ids
from app.artifact.template import TEMPLATE_NAME, render

#: `var/artifacts/<task_id>/<artifact_id>.md` -- next to `var/citadel.db`
#: (`app/config.py`'s `VAR_DIR`), not inside `data/` (the read-only RAG
#: corpus) and not inside the isolated execution zone.
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "var" / "artifacts"


def generate_report_backend(request: Any) -> Mapping[str, Any]:
    """Render the real `maintenance_summary_v1` template and return enough
    for the caller to build the `Artifact` row (`artifact_id`, `path`,
    `provenance`).

    `request` is `app.tool_gateway.registry.ToolRequest`; typed as `Any` here
    only to avoid importing the Tool Gateway's registry module purely for a
    type hint (`app/tool_gateway/backends/echo.py` does the same).
    """
    task_id = request.capability.task_id
    evidence = list(request.arguments.get("evidence") or [])
    computed = request.arguments.get("computed")
    # BB-045: "selection" is a no-op -- `template` is read only to echo it
    # back in the result for the trace; there is nowhere else it could route
    # to, since `app.artifact.template.render` is the only template.
    template = request.arguments.get("template") or TEMPLATE_NAME
    revision_comment = request.arguments.get("revision_comment")

    body = render(evidence=evidence, computed=computed, revision_comment=revision_comment)

    artifact_id = ids.new_id(ids.ARTIFACT)
    artifact_dir = ARTIFACTS_DIR / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{artifact_id}.md"
    path.write_text(body, encoding="utf-8")

    provenance = [row["evidence_id"] for row in evidence if "evidence_id" in row]

    return {
        "artifact_id": artifact_id,
        "path": str(path),
        "provenance": provenance,
        "template": template,
        "revised": bool(revision_comment),
    }


def register() -> None:
    """Attach this backend the same way every other tool does (section 6.6)."""
    from app.policy import Tool
    from app.tool_gateway import register_backend

    register_backend(Tool.GENERATE_REPORT, generate_report_backend)
