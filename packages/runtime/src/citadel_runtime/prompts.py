"""What the planner and the acting agent are told, and the JSON shapes they must answer in.

Everything here is built from data -- the task's real goal, the tools the policy offers
this actor for this task (each with its declared schema), the templates' declared
sections -- and nothing names a particular tool, template or model. The planner sees the
goal verbatim and returns a plan of whatever length the goal needs; the acting agent
returns one action per turn.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

PLAN_SCHEMA_BASE: dict[str, Any] = {
    "type": "object",
    "required": ["understanding", "steps", "primary_capability", "deliverable"],
    "properties": {
        "understanding": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
        "primary_capability": {"type": "string", "enum": ["reasoning", "code_generation"]},
        "deliverable": {"type": "string"},
    },
}


def plan_schema(template_ids: Sequence[str]) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(json.dumps(PLAN_SCHEMA_BASE))
    schema["properties"]["deliverable"]["enum"] = ["none", *template_ids]
    return schema


def action_schema(tool_names: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["thought", "action"],
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": ["call_tool", "finish", "replan"]},
            "tool": {"type": "string", "enum": list(tool_names) or ["none"]},
            "arguments": {"type": "object"},
            "answer": {"type": "string"},
            "new_plan": {"type": "array", "items": {"type": "string"}},
        },
    }


def _compact_schema(schema: Mapping[str, Any]) -> str:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = []
    for key, spec in properties.items():
        kind = spec.get("type", "any") if isinstance(spec, Mapping) else "any"
        mark = "" if key in required else "?"
        parts.append(f"{key}{mark}: {kind}")
    return "{" + ", ".join(parts) + "}"


def describe_tools(tools: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for tool in tools:
        description = " ".join(str(tool.get("description") or "").split())
        text = f"- {tool['name']} {_compact_schema(tool.get('schema') or {})}"
        if description:
            text += f" -- {description}"
        lines.append(text)
    return "\n".join(lines)


def describe_template(guide: Mapping[str, Any]) -> str:
    """A content skeleton for one template, from its declared sections."""
    skeleton: dict[str, Any] = {}
    notes = []
    for section in guide.get("sections") or []:
        key, kind = section["key"], section["type"]
        cite = " with citations like [E1]" if section.get("cited") else ""
        if kind in ("list",):
            skeleton[key] = [f"<one {key} item{cite}>"]
        elif kind == "table":
            skeleton[key] = [{"<column>": "<value>", "citations": ["E1"]}]
        elif kind == "date":
            skeleton[key] = "<YYYY-MM-DD>"
        elif kind == "value":
            skeleton[key] = f"<a number with its unit{cite}>"
        else:
            skeleton[key] = f"<{key}{cite}>"
        notes.append(f"{key} ({kind}{', required' if section.get('required') else ', optional'}"
                     f"{', must cite evidence' if section.get('cited') else ''})")
    return (
        f"template_id \"{guide['template_id']}\" ({guide.get('format')}) sections: " + "; ".join(notes)
        + "\ncontent skeleton: " + json.dumps(skeleton)
    )


def planner_messages(
    *,
    goal: str,
    user: Mapping[str, str],
    classification: str,
    tools: Sequence[Mapping[str, Any]],
    templates: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    template_lines = "\n".join(
        f"- {t['template_id']}: sections " + ", ".join(s["key"] for s in t.get("sections") or []) for t in templates
    )
    system = (
        "You are the planner for Citadel, an engineering workbench inside an air-gapped plant network. "
        "Read the user's goal and plan how to achieve it with the tools available. Plan only what this goal "
        "needs -- a one-step plan for a simple question is right; do not pad.\n\n"
        f"Tools available for this task:\n{describe_tools(tools)}\n\n"
        f"Deliverable templates:\n{template_lines or '- none'}\n\n"
        "Reply with JSON: understanding (one sentence restating the goal), steps (short imperative steps), "
        "primary_capability ('code_generation' if the goal is mainly to write or run code, otherwise "
        "'reasoning'), deliverable (a template id only if the goal asks for that document, else 'none')."
    )
    user_text = (
        f"Goal (verbatim from {user['username']}, {user['department']}; task classification {classification}):\n"
        f"{goal}"
    )
    return [("system", system), ("user", user_text)]


def actor_messages(
    *,
    goal: str,
    user: Mapping[str, str],
    classification: str,
    plan: Mapping[str, Any],
    tools: Sequence[Mapping[str, Any]],
    template_guide: str,
    deliverable_tool: str,
    evidence_index: Sequence[str],
    history: Sequence[str],
    step: int,
    max_steps: int,
    nudge: str = "",
) -> list[tuple[str, str]]:
    deliverable = ""
    if template_guide and deliverable_tool:
        deliverable = (
            f"\n\nThis task must produce a deliverable. When you have the evidence, call {deliverable_tool} with "
            "arguments {\"template_id\": ..., \"content\": {...}} using this shape:\n" + template_guide
            + "\nPut citations inline in the text, like \"CML-3 reads 9.2 mm [E2]\". Every number in a cited "
            "section must appear in the evidence it cites. If verification reports a failed tier, fix exactly "
            f"the issues listed and call {deliverable_tool} again. After a VERIFIED result, finish."
        )
    system = (
        "You are Citadel's agent, completing one task inside an air-gapped plant network. You act by choosing "
        "ONE action per turn and replying with a single JSON object and nothing else.\n\n"
        f"Task owner: {user['username']} ({user['department']}). Task classification: {classification}. "
        "The tools only ever show you what this person may read at this classification; anything withheld is "
        "reported as a count with a reason, and you must say so rather than guess at it.\n\n"
        f"Tools you may call:\n{describe_tools(tools)}\n\n"
        "Rules:\n"
        "1. Find facts with the tools before stating them; never rely on memory for plant data.\n"
        "2. Cite every fact and number with the evidence id it came from, in square brackets, e.g. [E2]. "
        "Only cite ids you were given.\n"
        "3. Never do arithmetic in your head: use a tool that records its working and cite the C id it returns.\n"
        "4. If a call is denied or fails, do not repeat it unchanged; adapt, or report what was withheld.\n"
        "5. When the goal is met, reply with action \"finish\" and an answer that gives the result with citations."
        f"{deliverable}\n\n"
        "Reply formats:\n"
        '{"thought": "...", "action": "call_tool", "tool": "<tool name>", "arguments": {...}}\n'
        '{"thought": "...", "action": "finish", "answer": "..."}\n'
        '{"thought": "...", "action": "replan", "new_plan": ["...", "..."]}'
    )
    plan_lines = "\n".join(f"{i}. {s}" for i, s in enumerate(plan.get("steps") or [], start=1))
    parts = [
        f"GOAL: {goal}",
        f"PLAN:\n{plan_lines or '(none)'}",
        "EVIDENCE YOU HOLD (cite these ids):\n" + ("\n".join(evidence_index) if evidence_index else "(none yet)"),
        "WHAT HAS HAPPENED SO FAR:\n" + ("\n".join(history) if history else "(nothing yet -- this is your first action)"),
    ]
    if nudge:
        parts.append(f"NOTE: {nudge}")
    parts.append(f"This is action {step} of at most {max_steps}. Choose the next action.")
    return [("system", system), ("user", "\n\n".join(parts))]


__all__ = [
    "PLAN_SCHEMA_BASE",
    "plan_schema",
    "action_schema",
    "describe_tools",
    "describe_template",
    "planner_messages",
    "actor_messages",
]
