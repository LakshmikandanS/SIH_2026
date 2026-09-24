"""What a finished task leaves behind in memory.

Two kinds of memory, written deliberately at a task's end -- never as a side effect of
a model turn (packages/memory/AGENTS.md):

* **episodic** -- what happened: the task, its outcome, an approver's decision and
  comment. Written as it happened, without a model, and never merged with other
  history: history is not deduplicated. One exception keeps it honest rather than
  repetitive: a task that ends again (after a revision) brings its own outcome up to
  date instead of adding a second one; the earlier wording stays in the event log.
* **semantic** -- durable facts the work established (a machine's condition, a cost, a
  workload), extracted by a model from the task's findings and answer, then run through
  the manager's mutation flow so a fact the workbench already knows is merged or
  updated rather than repeated.

Both live in the task owner's compartment: the task's classification, the owner's
department. A later task sees them only if the same predicate that guards documents
would let it.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from citadel_memory.manager import Candidate, Compartment, MemoryManager, Outcome


def _outcome_text(task: Mapping[str, Any], status: str, deliverables: Sequence[str]) -> str:
    title = " ".join(str(task.get("title") or task.get("goal") or "a task").split())[:120]
    who = task.get("submitted_by_name") or task.get("submitted_by") or "someone"
    text = f"{date.today().isoformat()}: {who} ran the task '{title}'; it ended {status.replace('_', ' ')}"
    if deliverables:
        text += f" with {', '.join(deliverables)}"
    return text + "."


def remember_task(
    manager: MemoryManager,
    *,
    task: Mapping[str, Any],
    status: str,
    answer: str,
    findings: Sequence[tuple[str, str]],
    facts: Sequence[str],
    deliverables: Sequence[str],
    classification: str,
    department: str,
    actor_id: str,
    on_model: Optional[Callable[[str, Any], None]] = None,
) -> list[Outcome]:
    compartment = Compartment(classification.upper(), (department,))
    task_id = str(task["id"])
    outcomes = [manager.propose(
        Candidate(content=_outcome_text(task, status, deliverables), memory_type="outcome", tier="episodic",
                  subject=str(task.get("title") or "")[:80] or None, certainty="certain"),
        compartment=compartment, actor_id=actor_id, task_id=task_id, use_model=False, replaces_same_task=True,
    )]
    parts = []
    if answer:
        parts.append(f"FINAL ANSWER:\n{answer}")
    for name, text in findings:
        if text:
            parts.append(f"FINDINGS OF {name}:\n{text}")
    if facts:
        parts.append("SHARED FACTS:\n" + "\n".join(f"- {f}" for f in facts))
    for candidate in manager.extract("\n\n".join(parts), classification=classification, actor_id=actor_id,
                                     task_id=task_id, on_model=on_model):
        outcomes.append(manager.propose(candidate, compartment=compartment, actor_id=actor_id, task_id=task_id,
                                        on_model=on_model))
    return outcomes


def remember_decision(
    manager: MemoryManager,
    *,
    task: Mapping[str, Any],
    approved: bool,
    comment: str,
    approver_name: str,
    artifact_title: str,
    classification: str,
    department: str,
    actor_id: str,
) -> Outcome:
    """An approver's decision is a lesson the next task should meet: especially a
    rejection, which says what the last draft of this kind was missing."""
    verdict = "approved" if approved else "rejected"
    text = f"{date.today().isoformat()}: {approver_name} {verdict} '{artifact_title}'"
    if comment.strip():
        text += f" with the comment: \"{' '.join(comment.split())[:300]}\""
    return manager.propose(
        Candidate(content=text + ".", memory_type="outcome" if approved else "rejection", tier="episodic",
                  subject=artifact_title[:80] or None, certainty="certain"),
        compartment=Compartment(classification.upper(), (department,)), actor_id=actor_id,
        task_id=str(task["id"]), use_model=False,
    )


__all__ = ["remember_task", "remember_decision"]
