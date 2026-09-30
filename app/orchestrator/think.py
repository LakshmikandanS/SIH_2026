"""THINK (design doc section 5.1): "given the step's action + current
evidence, decide the concrete arguments."

    THINK        -> given the step's action + current evidence, decide the
                    concrete arguments

**The critical seam of this whole module**, per the mission brief that
started this build: the planner's own `S2` (`python.execute`) `arguments.code`
is a hallucinated placeholder -- design doc section 5.2 says so explicitly
("<computed at runtime from S1's evidence>"), and testing the real §5.2
planning call against `hermes3` confirmed it comes back as literally
unrunnable code (e.g. `from search_engine import search`). **That code is
never executed.** `_think_python_execute` below is where the real code is
built, from S1's actual retrieved evidence -- this is THINK doing real work,
not a pass-through of the plan.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.orchestrator.schemas import PlanStepModel
from app.orchestrator.working_memory import WorkingMemory
from app.policy import Tool

#: Fallback only -- schema validation (`app.orchestrator.schemas`) already
#: requires a non-empty `query`, so this is defense in depth, not the normal
#: path. Matches design doc section 5.2's own plan example.
_DEFAULT_QUERY = "Pump P-101 maintenance history"

#: `%`-style, not an f-string: the generated code itself contains regex
#: `{4}`/`{2}` quantifiers and literal `%Y-%m-%d` strftime directives, both
#: of which would need escaping inside an f-string or `.format()` call.
#: `%`-formatting only needs `%%` escaped, which is simpler to read here.
#: Shape verified against the real sandbox in `tests/demos/step5_execution.py`
#: (design doc section 1.1 step 12) -- this is the same computation, reused
#: rather than reinvented.
_PYTHON_EXECUTE_TEMPLATE = '''\
import json, re
from datetime import datetime, timezone

text = %(text)s
dates = [
    datetime.strptime(m, "%%Y-%%m-%%d").date()
    for m in re.findall(r"^(\\d{4}-\\d{2}-\\d{2})", text, re.MULTILINE)
]
if dates:
    most_recent = max(dates)
    days_since = (datetime.now(timezone.utc).date() - most_recent).days
    result = {
        "most_recent": most_recent.isoformat(),
        "days_since": days_since,
        "records_found": len(dates),
    }
else:
    result = {"most_recent": None, "days_since": None, "records_found": 0}
print(json.dumps(result))
'''

_DEFAULT_TEMPLATE = "maintenance_summary_v1"


def _think_rag_search(step: PlanStepModel) -> dict[str, Any]:
    """S1: the planner's own free-text query is legitimate content (it is
    not code, it does not get executed) -- pass it through."""
    query = step.arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        query = _DEFAULT_QUERY
    return {"query": query}


def _think_python_execute(memory: WorkingMemory) -> dict[str, Any]:
    """S2: overwrite the planner's hallucinated `code` entirely. Build real
    Python from S1's actual retrieved evidence, already sitting in
    `memory.evidence` by the time this step's THINK runs."""
    evidence_text = memory.combined_evidence_text()
    code = _PYTHON_EXECUTE_TEMPLATE % {"text": json.dumps(evidence_text)}
    return {"code": code}


def _think_generate_report(
    step: PlanStepModel, memory: WorkingMemory, *, revision_comment: Optional[str]
) -> dict[str, Any]:
    """S3: the arguments the minimal `generate_report` seam
    (`app.orchestrator.report_backend`) needs -- the evidence and computed
    figures already gathered, plus the approver's comment when this call is
    part of section 5.3's one scoped revision."""
    template = step.arguments.get("template")
    if not isinstance(template, str) or not template.strip():
        template = _DEFAULT_TEMPLATE
    return {
        "template": template,
        "evidence": list(memory.evidence),
        "computed": memory.computed,
        "revision_comment": revision_comment,
    }


def think(
    step: PlanStepModel, memory: WorkingMemory, *, revision_comment: Optional[str] = None
) -> dict[str, Any]:
    """Dispatch on the step's action. One handler per action, all of this
    slice's three -- there is no generic fallback, deliberately: a plan step
    naming a fourth action would be a schema-validation bug upstream
    (`app.orchestrator.schemas.PlanStepModel`), not something THINK should
    guess about."""
    if step.action == Tool.RAG_SEARCH:
        return _think_rag_search(step)
    if step.action == Tool.PYTHON_EXECUTE:
        return _think_python_execute(memory)
    if step.action == Tool.GENERATE_REPORT:
        return _think_generate_report(step, memory, revision_comment=revision_comment)
    raise ValueError(f"no THINK handler for action {step.action!r}")
