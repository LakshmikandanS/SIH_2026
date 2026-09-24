"""doc.generate: a deliverable from a registered template plus content.

The model supplies content only -- section by section, with inline citations like
[E2] or [C1] -- and never layout. Citations are resolved to what this task was
actually given; the deliverables package renders into the fixed template and runs the
four-tier verification ladder. The reply says, per tier, what passed, what failed and
which numbers could not be traced, so a failed draft can be revised precisely.
"""

from __future__ import annotations

from typing import Any

from citadel_contracts.domain import Resource
from citadel_deliverables import TaskFacts, cited_ids, generate
from citadel_knowledge import evidence

from citadel_tools.context import Invocation, ResourceNotFound, ToolContext, ToolOutput
from citadel_tools.plugins import ToolPlugin, task_resource


def _template(ctx: ToolContext, template_id: str) -> Any:
    for template in ctx.registry.templates:
        if template.id == template_id:
            return template
    known = ", ".join(t.id for t in ctx.registry.templates)
    raise ResourceNotFound(f"no template {template_id!r}; templates are: {known}")


def _resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    template = _template(ctx, str(args["template_id"]))
    return task_resource(ctx, "deliverable", template.id)


def template_guide(ctx: ToolContext) -> list[dict[str, Any]]:
    """What each template asks for -- given to the planner so content is shaped to the
    declaration rather than guessed."""
    guide = []
    for template in ctx.registry.templates:
        guide.append({
            "template_id": template.id,
            "description": template.description or "",
            "format": template.format,
            "approval_block": template.approval_block,
            "sections": [
                {"key": s.key, "type": s.type, "required": s.required, "cited": s.cited,
                 **({"min_items": s.min_items} if s.min_items else {}),
                 **({"left_out_when_empty": True} if s.omit_when_empty else {})}
                for s in template.sections
            ],
        })
    return guide


def _run(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    ctx.boundary.verify(invocation, _resource(ctx, args))
    template = _template(ctx, str(args["template_id"]))
    content = dict(args["content"])
    resolved = evidence(ctx.db, ctx.task_id, cited_ids(template, content))
    generated = generate(
        ctx.db,
        ctx.data_dir,
        ctx.registry_dir,
        template,
        content,
        task=TaskFacts(ctx.task_id, ctx.task_classification, ctx.goal),
        author=ctx.user,
        evidence=resolved,
        audit=ctx.audit,
        revision_note=ctx.revision_note,
    )
    verification = generated.verification
    tiers = [
        {"tier": t.tier, "name": t.name, "status": t.status, "issues": t.issues[:6]}
        for t in verification.tiers
    ]
    data: dict[str, Any] = {
        "artifact_id": generated.artifact_id,
        "artifact_status": generated.status,
        "filename": generated.filename,
        "version": generated.version,
        "verification": tiers,
        "flagged_claims": verification.flagged_claims[:10],
    }
    if generated.status == "VERIFIED":
        data["next"] = (
            "The deliverable passed verification. Finish the task and mention any flagged claims."
            if not verification.flagged_claims
            else "The deliverable passed tiers 1-3; the flagged numbers could not be traced to the evidence they cite. "
            "Either fix them (cite the evidence that states them, or compute them with calc.evaluate and cite the C id) "
            "and generate again, or finish and state them as unverified."
        )
    else:
        data["next"] = "Fix every issue listed under the failed tier(s) and call doc.generate again."
    failed = [str(t["name"]) for t in tiers if t["status"] == "fail"]
    summary = (
        f"{template.id} v{generated.version}: {generated.status}"
        + (f" (failed: {', '.join(failed)})" if failed else "")
        + (f"; {len(verification.flagged_claims)} number(s) flagged" if verification.flagged_claims else "")
    )
    return ToolOutput(
        data=data,
        summary=summary,
        artifacts=[generated.artifact_id],
        detail={"generated": generated.to_dict()},
    )


PLUGINS = {"doc.generate": ToolPlugin("doc.generate", _resource, _run)}

__all__ = ["PLUGINS", "template_guide"]
