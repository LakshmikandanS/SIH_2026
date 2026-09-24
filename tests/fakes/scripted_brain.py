"""A scripted stand-in for a model that recognises Citadel's own prompts.

The fake Ollama answers every request through a `brain(model, messages, schema)`; this
one reads the planner and agent prompts the runtime actually builds -- the goal, the
evidence it holds, what has happened so far, any note -- and answers the way a
competent model would for the demonstration goals. It does not see the database or any
Citadel object: only the prompt text, exactly as a real model would. That is what makes
the end-to-end tests exercise the real loop, the real chokepoint and the real tools
rather than a shortcut around them.

Besides the five acceptance targets it plays the workbench scenario: a report on lathe
L-1 that the planner splits across three helper agents (the organisation's records, the
offline reference library, and a policy check that waits for the records), questions
typed at the command line (/ask: what changed in a policy, what the work is waiting
for), and the memory manager's two decisions (what to remember from finished work, and
how a new memory changes what is already remembered).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from fake_ollama import minimal_instance
from stack_fixtures import vision_aware_brain

_EVIDENCE_LINE = re.compile(r"^([EC]\d+)(?: \((.*?)\))?: (.*)$")

#: The three helper agents the planner hands the lathe report to (their goals are what
#: the helpers' prompts carry, so the brain recognises each by its own words).
LATHE_RECORDS = ("From the organisation's records, establish lathe L-1's condition, repair cost, replacement "
                 "cost and workload.")
LATHE_REFERENCE = ("From the offline reference library, compare CNC turning centres with retrofitting a "
                   "conventional lathe.")
LATHE_POLICY = ("Check lathe L-1 and requisition PR-2026-0418 against company policy as it stands today, "
                "including what changed since last month.")


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


def _passages(prompt: str) -> dict[str, str]:
    """The full text the tools showed for each evidence id in the recent results -- a
    model reads the passages, not just the one-line index."""
    found: dict[str, str] = {}
    for raw in re.findall(r"^  result: (.*)$", prompt, re.M):
        try:
            value = json.loads(raw.replace(" ...[truncated]", ""))
        except ValueError:
            continue
        for passage in (value.get("passages") or []) if isinstance(value, dict) else []:
            if isinstance(passage, dict) and passage.get("evidence_id"):
                found[str(passage["evidence_id"])] = f"{passage.get('document', '')} {passage.get('text', '')}"
    return found


def _find(prompt: str, held: Sequence[tuple[str, str, str]], *needles: str) -> Optional[str]:
    """The evidence id whose passage says all of `needles`."""
    for eid, text in _passages(prompt).items():
        if all(n.lower() in text.lower() for n in needles):
            return eid
    return _pick(held, *needles)


def _team_id(prompt: str, phrase: str) -> Optional[str]:
    """The id a teammate cited for a fact: the first citation after the phrase in their findings."""
    findings = _section(prompt, "FINDINGS FROM YOUR TEAM")
    index = findings.find(phrase)
    if index < 0:
        return None
    match = re.search(r"\[([EC]\d+)\]", findings[index: index + 260])
    return match.group(1) if match else None


def _doc_id(prompt: str, title_start: str) -> Optional[str]:
    match = re.search(rf'"document": "{re.escape(title_start)}[^"]*", "document_id": "([0-9a-f-]{{36}})"', prompt)
    return match.group(1) if match else None


def _note(prompt: str) -> str:
    note = re.search(r"^NOTE: (.*)$", prompt, re.M)
    return note.group(1) if note else ""


def _steer(prompt: str) -> Optional[str]:
    """What a person said to the lead: delivered once as a NOTE, and kept in the shared
    state (a note addressed to it) for every turn after."""
    match = re.search(r'says to you: "([^"]+)"', _note(prompt))
    if match:
        return match.group(1)
    shared = re.findall(r"^- note by person \S+ \(to the lead agent\): (.*)$", _section(prompt, "SHARED STATE (from people and agents on this task)"), re.M)
    return shared[-1] if shared else None


def _cite(*ids: Optional[str]) -> str:
    return "".join(f"[{i}]" for i in dict.fromkeys(i for i in ids if i))


def _any(held: Sequence[tuple[str, str, str]]) -> Optional[str]:
    return held[0][0] if held else None


# -- the five acceptance targets and the generic path -----------------------------------------


_REPORT_REQUEST = re.compile(r"^\s*(?:write |prepare |draft )?(?:a |the )?report\b|\b(?:write|prepare|draft) (?:a|the) report\b"
                             r"|\breport (?:on|about|covering|for)\b")


def _asks_for_report(lowered: str) -> bool:
    """'Report on lathe L-1...', 'write a report about...' -- not 'the stamp on the report'."""
    return bool(_REPORT_REQUEST.search(lowered)) and "approval note" not in lowered


def _act(goal: str, prompt: str, tools: Sequence[str]) -> dict[str, Any]:
    held = _evidence(prompt)
    done = _history(prompt)
    note_text = _note(prompt)
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

    if _asks_for_report(lowered):
        return _report_lead(goal, prompt, held, done, calcs)

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


# -- the workbench scenario: lathe L-1 ------------------------------------------------------------


def _helper(goal: str, prompt: str) -> dict[str, Any]:
    held = _evidence(prompt)
    done = _history(prompt)
    if goal.startswith("From the organisation's records"):
        searches = done.count("docs.search")
        if searches == 0:
            return {"thought": "Find L-1's condition report.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "lathe L-1 condition spindle runout repair cost replacement cost breakdowns"}}
        if searches == 1:
            return {"thought": "Find the shop's workload.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "lathe production log utilisation scrap backlog overtime"}}
        l1 = _pick(held, "condition report") or _any(held)
        log = _pick(held, "production log") or l1
        runout = _find(prompt, held, "runout", "0.045") or l1
        guard = _find(prompt, held, "not interlocked") or l1
        cost = _find(prompt, held, "3.9 lakh") or l1
        use = _find(prompt, held, "78%") or l1
        backlog = _find(prompt, held, "320 jobs") or log
        if "state.note" not in done:
            return {"thought": "Tell the team the cost figures now.", "action": "call_tool", "tool": "state.note",
                    "arguments": {"kind": "fact", "evidence": [cost],
                                  "content": f"L-1's repair cost over 12 months is INR 3.9 lakh against a like-for-like "
                                             f"replacement cost of INR 12.5 lakh {_cite(cost)}"}}
        return {"thought": "My part is done.", "action": "finish",
                "answer": f"L-1 (commissioned 2009) shows spindle runout of 0.045 mm against a 0.02 mm limit and 0.12 mm "
                          f"of bed wear {_cite(runout)}. Its chuck guard is not interlocked {_cite(guard)}. Repair cost "
                          f"over 12 months is INR 3.9 lakh against a replacement cost of INR 12.5 lakh {_cite(cost)}. "
                          f"Utilisation is 78% {_cite(use)}; the shop's turning backlog is 320 jobs {_cite(backlog)}."}
    if goal.startswith("From the offline reference library"):
        if "web.search" not in done:
            return {"thought": "Search the reference library.", "action": "call_tool", "tool": "web.search",
                    "arguments": {"query": "CNC turning centre versus conventional lathe retrofit cost cycle time swing"}}
        digest = _pick(held, "technology digest") or _any(held)
        retrofit = _find(prompt, held, "retrofit", "35 to 50%") or digest
        cycle = _find(prompt, held, "50 to 65%") or digest
        catalogue = _find(prompt, held, "KT-2A", "400 mm") or _pick(held, "catalogue") or digest
        return {"thought": "Report what the literature says.", "action": "finish",
                "answer": f"A CNC retrofit costs 35 to 50% of a new turning centre and does not correct bed wear above "
                          f"0.10 mm {_cite(retrofit)}. CNC turning centres cut cycle time by 50 to 65% on batch work "
                          f"{_cite(cycle)}. Swing: DT-250 450 mm, KT-2A 400 mm, WP-T20 500 mm {_cite(catalogue)}."}
    if goal.startswith("Check lathe L-1"):
        searches = done.count("docs.search")
        if searches == 0:
            return {"thought": "Find Policy 1 as it stands.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "Policy 1 capital equipment replacement repair cost quotations approval"}}
        if "docs.diff" not in done:
            document = _doc_id(prompt, "Policy 1")
            if document:
                return {"thought": "What changed in Policy 1 since last month?", "action": "call_tool",
                        "tool": "docs.diff", "arguments": {"document_id": document}}
        if searches == 1:
            return {"thought": "Check the guarding policy for lathes.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": "Policy 2 lathe chuck guard interlocked spindle drive"}}
        now = _pick(held, "later wording", "30%") or _pick(held, "policy 1", "revision: 2") or _pick(held, "policy 1")
        before = _pick(held, "earlier wording", "40%")
        quotes = _pick(held, "later wording", "three quotations") or now
        guard = _find(prompt, held, "policy 2", "interlocked") or _pick(held, "policy 2")
        requisition = _find(prompt, held, "quotations attached") or _pick(held, "requisition")
        l1 = _team_id(prompt, "chuck guard is not interlocked") or _pick(held, "condition report")
        if "state.note" not in done:
            return {"thought": "A conflict a person must decide on.", "action": "call_tool", "tool": "state.note",
                    "arguments": {"kind": "question", "evidence": [e for e in (requisition, quotes) if e],
                                  "content": "Policy conflict: requisition PR-2026-0418 attaches 1 quotation for INR 18.6 "
                                             f"lakh {_cite(requisition)}, but Policy 1 now requires three quotations above "
                                             f"INR 10 lakh {_cite(quotes)}. Should procurement obtain two more before "
                                             "the report goes for approval?"}}
        return {"thought": "My part is done.", "action": "finish",
                "answer": f"Policy 1 today qualifies a machine for replacement when its 12-month repair cost exceeds 30% "
                          f"of replacement cost {_cite(now)}; last month the threshold was 40% {_cite(before)}. Above INR "
                          f"10 lakh it now needs three quotations {_cite(quotes)}, but PR-2026-0418 attaches 1 "
                          f"{_cite(requisition)}. Policy 2 forbids running a lathe without an interlocked chuck guard "
                          f"{_cite(guard)}, and L-1's guard is not interlocked {_cite(l1)}."}
    if "docs.search" not in done:
        return {"thought": "Search for my part.", "action": "call_tool", "tool": "docs.search", "arguments": {"query": goal[:200]}}
    return {"thought": "Report what I have.", "action": "finish",
            "answer": f"Findings for my part {_cite(_any(held))}." if held else "I found nothing for my part."}


_LEADING = re.compile(r"^(?:and|or|only|its|their|the|a|an|on|about)\s+", re.I)


def _topics(goal: str) -> list[str]:
    """What the person asked the report to cover, in their words and their order:
    'Report on X: its condition and workload, the options, and a recommendation' ->
    ['condition and workload', 'options', 'recommendation']."""
    covering = re.search(r"\b(?:covering|including|that covers|which covers)\b\s+(.*)$", goal, re.I)
    text = covering.group(1) if covering else goal.split(":", 1)[1] if ":" in goal else ""
    parts = [p for p in re.split(r",\s*|;\s*", text) if p.strip()]
    if len(parts) == 1:
        parts = re.split(r"\s+and\s+", parts[0])
    topics = []
    for part in parts:
        topic = part.strip().rstrip(".")
        while _LEADING.match(topic):
            topic = _LEADING.sub("", topic, count=1)
        if topic:
            topics.append(topic)
    return topics or ["findings"]


def _heading(topic: str) -> str:
    return topic[:1].upper() + topic[1:]


def _report_ids(prompt: str, held: Sequence[tuple[str, str, str]]) -> dict[str, Optional[str]]:
    """Which evidence supports which fact: what a teammate cited for it, else a passage
    this agent read that says it."""

    def get(phrase: str, *needles: str) -> Optional[str]:
        return _team_id(prompt, phrase) or (_find(prompt, held, *needles) if needles else None)

    l1 = _pick(held, "condition report")
    return {
        "runout": get("spindle runout", "runout", "0.045") or l1,
        "guard": get("chuck guard is not interlocked", "not interlocked") or l1,
        "cost": get("INR 3.9 lakh", "3.9 lakh") or l1,
        "use": get("Utilisation is 78%", "78%") or l1,
        "backlog": get("backlog is 320 jobs", "320 jobs") or _pick(held, "production log"),
        "now": get("exceeds 30%") or _pick(held, "later wording", "30%"),
        "before": get("threshold was 40%") or _pick(held, "earlier wording", "40%"),
        "quotes": get("three quotations") or _pick(held, "later wording", "three quotations"),
        "requisition": get("attaches 1", "quotations attached") or _pick(held, "requisition"),
        "guard_policy": get("forbids running a lathe", "policy 2", "interlocked"),
        "retrofit": get("retrofit costs 35 to 50%", "retrofit", "35 to 50%"),
        "cycle": get("cycle time by 50 to 65%", "50 to 65%"),
        "catalogue": get("Swing:", "KT-2A", "400 mm") or _pick(held, "catalogue"),
        "training": _find(prompt, held, "weeks of training"),
    }


def _line(text: str, *ids: Optional[str]) -> Optional[str]:
    """A sentence only if something it can cite supports it."""
    cites = [i for i in ids if i]
    return f"{text} {_cite(*cites)}" if cites else None


def _topic_lines(topic: str, ids: Mapping[str, Optional[str]], calc: Optional[str]) -> list[str]:
    t = topic.lower()
    lines: list[Optional[str]] = []
    if any(w in t for w in ("condition", "state", "health", "safety")):
        lines += [_line("Spindle runout is 0.045 mm against an acceptance limit of 0.02 mm.", ids["runout"]),
                  _line("Bed wear is 0.12 mm near the headstock.", ids["runout"]),
                  _line("The chuck guard is not interlocked with the spindle drive.", ids["guard"])]
    if any(w in t for w in ("workload", "utilisation", "utilization", "demand", "backlog")):
        lines += [_line("L-1 was in use 78% of available hours over two shifts.", ids["use"]),
                  _line("The shop's turning backlog is 320 jobs.", ids["backlog"])]
    if any(w in t for w in ("cost", "repair")):
        lines += [_line("Repair cost over 12 months is INR 3.9 lakh against a replacement cost of INR 12.5 lakh.",
                        ids["cost"]),
                  _line("That is 31.2% of the replacement cost.", calc)]
    if any(w in t for w in ("polic", "complian", "rule")):
        lines += [_line("Policy 1 now qualifies a machine for replacement when its repair cost over 12 months exceeds "
                        "30% of replacement cost.", ids["now"]),
                  _line("Until last month the threshold was 40%.", ids["before"]),
                  _line("L-1's repair cost is 31.2% of its replacement cost, so it now qualifies.", calc),
                  _line("Above INR 10 lakh three quotations are required, and requisition PR-2026-0418 attaches 1.",
                        ids["quotes"], ids["requisition"]),
                  _line("Policy 2 forbids running a lathe without an interlocked chuck guard.", ids["guard_policy"])]
    if any(w in t for w in ("option", "vendor", "alternative", "replace")):
        lines += [_line("- Replace L-1 with a CNC turning centre: the DT-250 is quoted at INR 18.6 lakh.",
                        ids["requisition"]),
                  _line("- A CNC retrofit of L-1 costs 35 to 50% of a new turning centre but does not correct bed "
                        "wear above 0.10 mm.", ids["retrofit"]),
                  _line("- The KT-2A's 400 mm swing is below L-1's 450 mm.", ids["catalogue"])]
    if "training" in t or "operator" in t:
        lines += [_line("Conventional machinists typically need 3 to 4 weeks of training on a CNC turning centre.",
                        ids["training"])]
    return [line for line in lines if line]


def _generic_lines(topic: str, prompt: str, held: Sequence[tuple[str, str, str]]) -> list[str]:
    """For a subject the script knows nothing about: the passage sharing most words with
    the topic, quoted, with its id -- enough to show the shape follows the request."""
    words = {w for w in re.findall(r"[a-z0-9-]+", topic.lower()) if len(w) > 2}
    best: Optional[tuple[str, str]] = None
    best_score = 0
    for eid, text in [*_passages(prompt).items(), *((h[0], h[2]) for h in held)]:
        score = sum(1 for w in words if w in text.lower())
        if score > best_score:
            best, best_score = (eid, text), score
    if best is None:
        return []
    sentence = re.split(r"(?<=[.;])\s+", " ".join(best[1].split()))[0][:220].rstrip(".;")
    return [f"The records say: {sentence} [{best[0]}]."]


def _revision(prompt: str) -> tuple[str, Optional[int], dict[str, Any]]:
    """The standing revision request: what was asked, which version, its content."""
    brief = _section(prompt, "REVISION REQUESTED")
    asked = re.search(r'asked: "([^"]+)"', brief) or re.search(r'rejected it: "([^"]+)"', brief)
    version = re.search(r"CURRENT VERSION \(v(\d+) of", brief)
    body = brief.split("keeping what is not asked to change:\n", 1)
    content: dict[str, Any] = {}
    if len(body) == 2:
        try:
            parsed = json.loads(body[1].strip())
            content = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            content = {}
    return (asked.group(1) if asked else ""), (int(version.group(1)) if version else None), content


def _report_lead(goal: str, prompt: str, held: Sequence[tuple[str, str, str]], done: Sequence[str],
                 calcs: Sequence[str]) -> dict[str, Any]:
    lowered = goal.lower()
    lathe = "lathe" in lowered
    topics = _topics(goal)
    asked, from_version, current = _revision(prompt)
    history = _section(prompt, "WHAT HAS HAPPENED SO FAR")
    generated = [int(v) for v in re.findall(r"report v(\d+): VERIFIED", history)]
    last = _last_result(prompt)
    if done and done[-1] == "doc.generate" and last.get("artifact_status") == "VERIFIED" and (
            from_version is None or (generated and max(generated) > from_version)):
        gist = (f" Lathe L-1's repair cost over 12 months is 31.2% of its replacement cost [{calcs[-1]}]."
                if lathe and calcs else "")
        return {"thought": "The report is verified.", "action": "finish",
                "answer": f"The report is written and verified (v{last.get('version')}), with sections: "
                          + "; ".join(_heading(t) for t in topics if "recommend" not in t.lower()) + "." + gist}
    team = bool(_section(prompt, "FINDINGS FROM YOUR TEAM").strip())
    wants = " ".join(topics).lower() + " " + asked.lower()
    needs_calc = lathe and any(w in wants for w in ("polic", "cost", "repair", "complian"))
    # Working alone, find the facts first: the records, then the reference library.
    if not team and "docs.search" not in done:
        return {"thought": "Find what the records say.", "action": "call_tool", "tool": "docs.search",
                "arguments": {"query": (goal.split(":", 1)[0] + " " + " ".join(topics))[:200]}}
    if not team and lathe and any(w in wants for w in ("option", "vendor", "alternative")) and "web.search" not in done:
        return {"thought": "Look at the reference library for the options.", "action": "call_tool", "tool": "web.search",
                "arguments": {"query": "CNC turning centre retrofit conventional lathe swing price"}}
    if needs_calc and not calcs:
        return {"thought": "Compute the repair-cost ratio with recorded working.", "action": "call_tool",
                "tool": "calc.evaluate", "arguments": {"expression": "repair cost ratio = 3.9 / 12.5 * 100"}}
    calc = calcs[-1] if calcs else None
    ids = _report_ids(prompt, held)
    added = re.search(r"\badd (?:a )?section (?:on|about|for|covering) (.+?)[.!]?$", asked, re.I)
    if added and lathe and "training" in added.group(1).lower() and not ids["training"]:
        if "web.search" not in done[-2:]:
            return {"thought": "Find evidence for the section they asked for.", "action": "call_tool",
                    "tool": "web.search", "arguments": {"query": f"{added.group(1)} CNC turning centre"}}
    content: dict[str, Any]
    if current:
        content = json.loads(json.dumps(current))  # revise THEIR version, edits and all
    else:
        body = []
        for topic in topics:
            if "recommend" in topic.lower():
                continue
            lines = _topic_lines(topic, ids, calc) if lathe else _generic_lines(topic, prompt, held)
            if lines:
                body.append({"heading": _heading(topic), "text": "\n".join(lines)})
        summary = (_line("Lathe L-1's spindle runout of 0.045 mm is over the 0.02 mm limit"
                         + (", and its repair cost over 12 months is 31.2% of its replacement cost." if calc else "."),
                         ids["runout"], calc) if lathe else None)
        if summary is None:
            summary = next((b["text"].splitlines()[0] for b in body), "The records hold little on this.")
        content = {"title": (goal.split(":", 1)[0] if ":" in goal else goal)[:110].strip(), "summary": summary,
                   "body": body}
        if any("recommend" in t.lower() for t in topics) and lathe:
            content["recommendations"] = [
                "Replace L-1 with a CNC turning centre once procurement holds the three quotations Policy 1 now requires.",
                "Fit an interlocked chuck guard, or stop using L-1 until one is fitted."]
        if lathe and ids["quotes"] and any("polic" in t.lower() for t in topics):
            content["open_questions"] = ["Should procurement obtain two more quotations for PR-2026-0418 first?"]
    if added:
        topic = added.group(1).strip().rstrip(".")
        lines = _topic_lines(topic, ids, calc) if lathe else _generic_lines(topic, prompt, held)
        if lines:
            content.setdefault("body", []).append({"heading": _heading(topic), "text": "\n".join(lines)})
    elif asked and any(w in asked.lower() for w in ("shorter", "shorten", "concise")):
        content["summary"] = re.split(r"(?<=[.])\s+", str(content.get("summary") or ""))[0]
    elif asked:
        content.setdefault("open_questions", []).append(f"Asked for in revision: {asked}")
    steer = _steer(prompt)
    if steer and all(steer not in str(q) for q in content.get("open_questions") or []):
        content.setdefault("open_questions", []).append(f"Raised by a person while the work ran: {steer}")
    return {"thought": "Write the report the person asked for.", "action": "call_tool", "tool": "doc.generate",
            "arguments": {"template_id": "report", "content": content}}


def _ask(question: str, prompt: str) -> dict[str, Any]:
    held = _evidence(prompt)
    done = _history(prompt)
    lowered = question.lower()
    if "policy" in lowered and any(w in lowered for w in ("difference", "changed", "change", "differ")):
        if "docs.search" not in done:
            return {"thought": "Find the policy.", "action": "call_tool", "tool": "docs.search",
                    "arguments": {"query": question[:200]}}
        if "docs.diff" not in done:
            document = _doc_id(prompt, "Policy 1")
            if document:
                return {"thought": "Compare last month's issue with today's.", "action": "call_tool",
                        "tool": "docs.diff", "arguments": {"document_id": document}}
        diff = _last_result(prompt)
        parts = []
        for change in (diff.get("changes") or [])[:5]:
            before, after = change.get("before"), change.get("after")
            b_ids, a_ids = change.get("before_evidence") or [], change.get("after_evidence") or []
            if before and after and b_ids and a_ids:
                parts.append(f"\"{before}\" [{b_ids[0]}] became \"{after}\" [{a_ids[0]}]")
            elif after and a_ids:
                parts.append(f"added \"{after}\" [{a_ids[0]}]")
            elif before and b_ids:
                parts.append(f"removed \"{before}\" [{b_ids[0]}]")
        start = (diff.get("from") or {}).get("effective")
        end = (diff.get("to") or {}).get("effective")
        return {"thought": "Answer from the diff.", "action": "finish",
                "answer": f"Policy 1 changed between the issue effective {start} and the one effective {end}: "
                          + "; ".join(parts) + "."}
    if any(w in lowered for w in ("agent", "doing", "status", "waiting", "progress", "decided", "task")):
        if "workbench.inspect" not in done:
            words = re.sub(r"\b(what|is|the|are|agent|agents|doing|status|of|on|for|waiting|my|task|progress)\b", " ",
                           lowered)
            return {"thought": "Look at the work in progress.", "action": "call_tool", "tool": "workbench.inspect",
                    "arguments": {"task": " ".join(words.split())[:80]} if words.strip() else {}}
        result = _last_result(prompt)
        agents = (result.get("task") or {}).get("agents") or result.get("agents") or []
        lines = [f"{a.get('name')} is {a.get('status')}" + (f" ({a.get('current_step')})" if a.get("current_step") else "")
                 for a in agents if isinstance(a, dict)]
        summary = "; ".join(lines) or str(result.get("summary") or "nothing is running")
        return {"thought": "Say where things stand.", "action": "finish", "answer": f"Where the work stands: {summary}."}
    if "docs.search" not in done:
        return {"thought": "Search.", "action": "call_tool", "tool": "docs.search", "arguments": {"query": question[:200]}}
    return {"thought": "Answer.", "action": "finish",
            "answer": f"From the permitted documents {_cite(_any(held))}." if held else "Nothing I may read answers that."}


def _plan(goal: str, team: bool) -> dict[str, Any]:
    lowered = goal.lower()
    if "approval note" in lowered:
        return {"understanding": "Produce an approval note for E-101 from the inspection evidence.",
                "steps": ["Search the inspection evidence", "Compute remaining life", "Generate the approval note"],
                "primary_capability": "reasoning", "deliverable": "approval-note"}
    if _asks_for_report(lowered):
        topics = [_heading(t) for t in _topics(goal)]
        plan: dict[str, Any] = {
            "understanding": f"A report covering: {', '.join(topics)}.",
            "steps": ["Find the facts for each part", "Write the report, one section per part asked for"],
            "primary_capability": "reasoning", "deliverable": "report",
        }
        if "lathe" in lowered:
            plan["assumptions"] = ["'The lathe' is lathe L-1 in machine shop sector 1, the one with an open requisition."]
        if team and "lathe" in lowered and "polic" in lowered:
            plan["steps"] = ["Take the helpers' findings", "Compute the repair-cost ratio", "Write the report"]
            plan["agents"] = [
                {"name": "records", "goal": LATHE_RECORDS, "focus": ["docs.search", "docs.read"], "depends_on": []},
                {"name": "reference", "goal": LATHE_REFERENCE, "focus": ["web.search"], "depends_on": []},
                {"name": "policy", "goal": LATHE_POLICY, "focus": ["docs.search", "docs.diff"], "depends_on": [1]},
            ]
        return plan
    if "python" in lowered or "script" in lowered:
        return {"understanding": "Compute remaining life with a script.", "steps": ["Write and run the script"],
                "primary_capability": "code_generation", "deliverable": "none"}
    return {"understanding": goal[:120], "steps": ["Find the relevant documents", "Answer with citations"],
            "primary_capability": "reasoning", "deliverable": "none"}


# -- the memory manager's two decisions -------------------------------------------------------------


def _extract(text: str) -> dict[str, Any]:
    """What is worth remembering from finished work: durable facts about named equipment."""
    memories = []
    if "L-1" in text and "31.2%" in text:
        memories.append({"content": "Lathe L-1 in machine shop sector 1 has a 12-month repair cost of 31.2% of its "
                                    "replacement cost (INR 3.9 lakh against INR 12.5 lakh).",
                         "memory_type": "equipment_fact", "tier": "semantic", "subject": "Lathe L-1",
                         "certainty": "certain", "evidence": re.findall(r"\b([EC]\d+)\b", text)[:3]})
    if "PR-2026-0418" in text and "quotation" in text:
        memories.append({"content": "Requisition PR-2026-0418 for a CNC turning centre carries one quotation; Policy 1 "
                                    "requires three above INR 10 lakh.",
                         "memory_type": "constraint", "tier": "semantic", "subject": "PR-2026-0418",
                         "certainty": "certain"})
    if "E-101" in text and "3.2 years" in text:
        memories.append({"content": "Heat exchanger E-101: CML-3 at 9.2 mm against a t-min of 8.4 mm leaves a "
                                    "remaining life of 3.2 years.",
                         "memory_type": "equipment_fact", "tier": "semantic", "subject": "E-101",
                         "certainty": "certain"})
    return {"decision": "store" if memories else "discard", "memories": memories}


def _mutate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Same subject: the newer wording updates the old memory. Otherwise it is new."""
    candidate = payload.get("candidate") or {}
    for memory in payload.get("existing_memories") or []:
        if candidate.get("subject") and memory.get("subject") == candidate.get("subject"):
            if memory.get("content") == candidate.get("content"):
                return {"operation": "ignore", "target": "none", "confidence": 0.9, "reason": "already remembered"}
            return {"operation": "update", "target": memory.get("ref"), "replacement": candidate.get("content"),
                    "confidence": 0.8, "reason": "the same subject, newer wording"}
    return {"operation": "create", "target": "none", "confidence": 0.9, "reason": "nothing remembered covers it"}


def citadel_brain(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
    properties = (schema or {}).get("properties") or {}
    if any(m.get("images") for m in messages):
        if "legible" in properties or ("text" in properties and "fields" in properties):
            return json.dumps({"text": "QA INSPECTED 14 AUG 2026 INSP. CELL", "legible": True,
                               "fields": [{"label": "Stamp", "value": "QA INSPECTED 14 AUG 2026"}]})
        return vision_aware_brain(model, messages, schema)
    user = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
    system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
    if "understanding" in properties:
        goal = user.split(":\n", 1)[-1]
        return json.dumps(_plan(goal, team="agents" in properties))
    if "action" in properties:
        tools = list((properties.get("tool") or {}).get("enum") or [])
        if "typed into the workbench's command line" in system:
            return json.dumps(_ask(_goal(user), user))
        if "YOUR PART:" in user:
            return json.dumps(_helper(_goal(user), user))
        return json.dumps(_act(_goal(user), user, tools))
    if "memories" in properties and "decision" in properties:
        return json.dumps(_extract(user))
    if "operation" in properties and "target" in properties:
        try:
            payload = json.loads(user)
        except ValueError:
            payload = {}
        return json.dumps(_mutate(payload if isinstance(payload, dict) else {}))
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


def with_hooks(brain: Callable[..., str], hooks: list[Callable[[str, str], None]]) -> Callable[..., str]:
    """Wrap a brain so a test can act at a precise moment of a run -- a person pausing the
    task while a helper agent is mid-step, say. Each hook sees (system, user) text."""

    def wrapped(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
        user = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
        system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
        for hook in list(hooks):
            hook(system, user)
        return brain(model, messages, schema)

    return wrapped


__all__ = ["citadel_brain", "recording", "with_hooks", "LATHE_RECORDS", "LATHE_REFERENCE", "LATHE_POLICY"]
