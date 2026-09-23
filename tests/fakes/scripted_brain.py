"""A scripted stand-in for a model that recognises Citadel's own prompts.

The fake Ollama answers every request through a `brain(model, messages, schema)`; this
one reads the planner and agent prompts the runtime actually builds -- the goal, the
evidence it holds, what has happened so far, any note -- and answers the way a
competent model would for the five demonstration goals. It does not see the database or
any Citadel object: only the prompt text, exactly as a real model would. That is what
makes the end-to-end tests exercise the real loop, the real chokepoint and the real
tools rather than a shortcut around them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from fake_ollama import minimal_instance
from stack_fixtures import vision_aware_brain

_EVIDENCE_LINE = re.compile(r"^([EC]\d+)(?: \((.*?)\))?: (.*)$")


def _section(text: str, title: str) -> str:
    match = re.search(rf"{re.escape(title)}:?\n(.*?)(?:\n\n[A-Z][A-Z ]+[A-Z]|\n\nNOTE:|\n\nThis is action|\Z)", text, re.S)
    return match.group(1) if match else ""


def _evidence(text: str) -> list[tuple[str, str, str]]:
    held = []
    for line in _section(text, "EVIDENCE YOU HOLD (cite these ids)").splitlines():
        match = _EVIDENCE_LINE.match(line.strip())
        if match:
            held.append((match.group(1), match.group(2) or "", match.group(3)))
    return held


def _history(text: str) -> list[str]:
    return re.findall(r"^Step \d+: (\S+) ", _section(text, "WHAT HAS HAPPENED SO FAR"), re.M)


def _last_result(text: str) -> dict[str, Any]:
    results = re.findall(r"^  result: (.*)$", text, re.M)
    if not results:
        return {}
    try:
        value = json.loads(results[-1].replace(" ...[truncated]", ""))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _goal(text: str) -> str:
    match = re.search(r"^GOAL: (.*)$", text, re.M)
    return match.group(1) if match else text


def _pick(held: Sequence[tuple[str, str, str]], *needles: str) -> Optional[str]:
    for eid, where, snippet in held:
        blob = f"{where} {snippet}".lower()
        if all(n.lower() in blob for n in needles):
            return eid
    return None


def _act(goal: str, prompt: str, tools: Sequence[str]) -> dict[str, Any]:
    held = _evidence(prompt)
    done = _history(prompt)
    note = re.search(r"^NOTE: (.*)$", prompt, re.M)
    note_text = note.group(1) if note else ""
    lowered = goal.lower()
    calcs = [eid for eid, _, _ in held if eid.startswith("C")]

    if "approval note" in lowered:
        if "docs.search" not in done:
            return {"thought": "Find the inspection data for E-101.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "E-101 CML-3 minimum thickness t-min corrosion rate remaining life"}}
        documents = [h for h in held if h[0].startswith("E")]
        reading = _pick(documents, "9.2") or _pick(documents, "cml-3") or _pick(documents, "ir-2026-0147") or (
            documents[0][0] if documents else "E1")
        if not calcs:
            return {"thought": "Compute remaining life with recorded working.", "action": "call_tool",
                    "tool": "calc.evaluate", "arguments": {"expression": "remaining life = (9.2 - 8.4) / 0.25"}}
        calc = calcs[-1]
        revising = "REJECTED" in note_text
        generated = done.count("doc.generate")
        last = _last_result(prompt)
        if done and done[-1] == "doc.generate" and last.get("artifact_status") == "VERIFIED" and not revising:
            return {"thought": "The note is verified.", "action": "finish",
                    "answer": f"Approval note generated and verified. CML-3 on E-101 measured 9.2 mm against a t-min of "
                              f"8.4 mm [{reading}]; remaining life is 3.2 years [{calc}]. It awaits approval."}
        recommendation = "Continue service; re-inspect CML-3 within 12 months."
        if revising or generated >= 1 and "deliverable" in note_text:
            recommendation = "Continue service; re-inspect CML-3 by ultrasonic thickness survey within 12 months."
        content = {
            "subject": "Continued service of heat exchanger E-101",
            "background": f"E-101 was inspected under IR-2026-0147 after the flange leak [{reading}].",
            "findings": [f"CML-3 measured a minimum wall thickness of 9.2 mm against a t-min of 8.4 mm [{reading}]",
                         f"The measured corrosion rate is 0.25 mm/year [{reading}]"],
            "analysis": f"At 0.25 mm/year the remaining life to t-min is 3.2 years [{reading}][{calc}].",
            "recommendation": recommendation,
            "annexures": [f"Inspection report IR-2026-0147 [{reading}]"],
        }
        return {"thought": "Draft the approval note from the evidence.", "action": "call_tool", "tool": "doc.generate",
                "arguments": {"template_id": "approval-note", "content": content}}

    if "python" in lowered or "script" in lowered:
        if "code.run" not in done:
            source = (
                "readings = {'CML-1': 10.1, 'CML-2': 9.8, 'CML-3': 9.2}\n"
                "t_min, rate = 8.4, 0.25\n"
                "worst = min(readings, key=readings.get)\n"
                "life = (readings[worst] - t_min) / rate\n"
                "print(f'{worst} remaining life {life:.1f} years')\n"
            )
            return {"thought": "Write and run the calculation.", "action": "call_tool", "tool": "code.run",
                    "arguments": {"source": source}}
        return {"thought": "Report the sandbox result.", "action": "finish",
                "answer": f"The worst location is CML-3 with a remaining life of 3.2 years [{calcs[-1] if calcs else 'C1'}]."}

    if "stamp" in lowered:
        if "docs.search" not in done:
            return {"thought": "Locate the scanned report.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "IR-2026-0147 inspection report E-101 stamp signature"}}
        if "vision.extract" not in done:
            document = (re.search(r'"document": "Inspection report[^"]*", "document_id": "([0-9a-f-]{36})"', prompt)
                        or re.search(r'"document_id": "([0-9a-f-]{36})"', prompt))
            return {"thought": "Re-read the stamp region with the vision model.", "action": "call_tool",
                    "tool": "vision.extract",
                    "arguments": {"document_id": document.group(1) if document else "missing", "page": 1,
                                  "bbox": [0.55, 0.70, 0.95, 0.95]}}
        vision = [eid for eid, _, snippet in held if "qa inspected" in snippet.lower() and eid.startswith("E")]
        return {"thought": "Report the stamp.", "action": "finish",
                "answer": f"The stamp reads QA INSPECTED 14 AUG 2026 [{vision[-1] if vision else held[-1][0]}]."}

    if "loop forever" in lowered:
        return {"thought": "Keep searching.", "action": "call_tool", "tool": "calc.evaluate",
                "arguments": {"expression": f"{len(done)} + 1"}}

    if "docs.search" not in done:
        return {"thought": "Search the corpus.", "action": "call_tool", "tool": "docs.search", "arguments": {"query": goal[:200]}}
    first = held[0][0] if held else "E1"
    return {"thought": "Answer from what was found.", "action": "finish",
            "answer": f"Here is what the permitted documents say [{first}]."}


def _plan(goal: str) -> dict[str, Any]:
    lowered = goal.lower()
    if "approval note" in lowered:
        return {"understanding": "Produce an approval note for E-101 from the inspection evidence.",
                "steps": ["Search the inspection evidence", "Compute remaining life", "Generate the approval note"],
                "primary_capability": "reasoning", "deliverable": "approval-note"}
    if "python" in lowered or "script" in lowered:
        return {"understanding": "Compute remaining life with a script.", "steps": ["Write and run the script"],
                "primary_capability": "code_generation", "deliverable": "none"}
    return {"understanding": goal[:120], "steps": ["Find the relevant documents", "Answer with citations"],
            "primary_capability": "reasoning", "deliverable": "none"}


def citadel_brain(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
    properties = (schema or {}).get("properties") or {}
    if any(m.get("images") for m in messages):
        if "legible" in properties or ("text" in properties and "fields" in properties):
            return json.dumps({"text": "QA INSPECTED 14 AUG 2026 INSP. CELL", "legible": True,
                               "fields": [{"label": "Stamp", "value": "QA INSPECTED 14 AUG 2026"}]})
        return vision_aware_brain(model, messages, schema)
    user = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
    if "understanding" in properties:
        goal = user.split(":\n", 1)[-1]
        return json.dumps(_plan(goal))
    if "action" in properties:
        tools = list((properties.get("tool") or {}).get("enum") or [])
        return json.dumps(_act(_goal(user), user, tools))
    if schema is not None:
        return json.dumps(minimal_instance(schema))
    return "ok"


def recording(brain: Callable[..., str], seen: list[tuple[str, str]]) -> Callable[..., str]:
    """Wrap a brain to record (model tag, purpose-ish) for routing assertions."""

    def wrapped(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
        properties = (schema or {}).get("properties") or {}
        purpose = "plan" if "understanding" in properties else "act" if "action" in properties else "other"
        seen.append((model, purpose))
        return brain(model, messages, schema)

    return wrapped


__all__ = ["citadel_brain", "recording"]
