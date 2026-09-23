"""The agent loop: plan, then act one tool call at a time, observing, until done.

    plan -> select a tool -> invoke it through the chokepoint -> observe
         -> decide: continue | retry | replan | finish

The planner sees the real goal and returns a plan of whatever length it needs. Each
action is one JSON object, constrained to the schema of the tools the policy offers
this actor for this task. Observations are handled by their shape -- anything carrying
an `evidence_id` joins the evidence the agent may cite -- never by which tool produced
them, so a new tool needs no change here.

Budgets (steps, tokens, wall clock) and cancellation are checked before every model
call. Every step is journalled before the next begins; a worker that dies mid-task
leaves a journal another worker resumes from.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from citadel_contracts.domain import User
from citadel_gateway import Message, NoEligibleModel, ProviderError, RoutingRequest, StructuredOutputError
from citadel_memory import WorkingMemory
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.identity.store import get_user_by_external_identity
from citadel_platform.registry import Registry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer, task_context
from citadel_tools import Chokepoint, DataBoundary, SandboxRunner, ToolContext, actor_facts_for_task
from citadel_tools.doc import template_guide

from citadel_runtime import prompts
from citadel_runtime.tasks import Journal, get_task, heartbeat, is_cancel_requested, set_status

_CITE = re.compile(r"\b([EC]\d+)\b")
_CITE_GROUP = re.compile(r"\[((?:[EC]\d+)(?:\s*[,;]\s*[EC]\d+)*)\]")
RECENT_IN_FULL = 4
OBSERVATION_CHARS = 2600


@dataclass(frozen=True)
class BudgetLimits:
    max_steps: int = 14
    max_tokens: int = 150_000
    max_seconds: int = 1500
    revision_steps: int = 8


class TaskCancelled(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, which: str, used: Any, limit: Any) -> None:
        super().__init__(f"{which} budget exceeded ({used} of {limit})")
        self.which, self.used, self.limit = which, used, limit


@dataclass
class Runtime:
    """Everything a worker needs, built once per process."""

    db: Database
    registry: Registry
    registry_dir: Path
    data_dir: DataDir
    gateway: Any  # citadel_gateway.Gateway
    chokepoint: Chokepoint
    boundary: DataBoundary
    sandbox: Optional[SandboxRunner] = None
    audit: Optional[AuditLog] = None
    tracer: Optional[Tracer] = None
    limits: BudgetLimits = field(default_factory=BudgetLimits)
    lookup_user: Optional[Callable[[str], Optional[User]]] = None

    def user(self, external_identity: str) -> Optional[User]:
        if self.lookup_user is not None:
            return self.lookup_user(external_identity)
        return get_user_by_external_identity(self.db.env, external_identity)


def _truncate(value: Any, limit: int) -> Any:
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _harvest(obj: Any, found: dict[str, str]) -> None:
    """Every item carrying an evidence id, wherever it sits in a tool's output."""
    if isinstance(obj, Mapping):
        eid = obj.get("evidence_id")
        if isinstance(eid, str) and _CITE.fullmatch(eid):
            where = ""
            if obj.get("document"):
                where = f" ({str(obj['document'])[:70]}{', p' + str(obj['page']) if obj.get('page') else ''})"
            if obj.get("text"):
                snippet = " ".join(str(obj["text"]).split())[:220]
            elif obj.get("result") is not None:
                snippet = f"{obj.get('expression', '')} = {obj.get('result')}".strip()
            else:
                snippet = ""
            found[eid] = f"{eid}{where}: {snippet}"
        for value in obj.values():
            _harvest(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _harvest(value, found)


def cited(text: str) -> list[str]:
    ids: list[str] = []
    for group in _CITE_GROUP.findall(text or ""):
        for eid in _CITE.findall(group):
            if eid not in ids:
                ids.append(eid)
    return ids


class Agent:
    def __init__(self, rt: Runtime, task: Mapping[str, Any], worker_id: str) -> None:
        self.rt = rt
        self.task_id = str(task["id"])
        self.goal = str(task["goal"])
        self.classification = str(task["classification"]).upper()
        self.previous_status = task.get("previous_status")
        self.revision_count = int(task.get("revision_count") or 0)
        self.worker_id = worker_id
        self.agent_id = f"agent-{self.task_id[:8]}"
        user = rt.user(str(task["submitted_by"]))
        if user is None:
            raise RuntimeError(f"task {self.task_id} was submitted by an identity that no longer exists")
        self.user = user
        self.journal = Journal(rt.db, self.task_id)
        self.memory = WorkingMemory(rt.db, self.task_id)
        self.ctx = ToolContext(
            task_id=self.task_id,
            agent_id=self.agent_id,
            user=user,
            actor=actor_facts_for_task(user, rt.registry, self.classification),
            task_classification=self.classification,
            db=rt.db,
            data_dir=rt.data_dir,
            registry=rt.registry,
            registry_dir=rt.registry_dir,
            boundary=rt.boundary,
            gateway=rt.gateway,
            audit=rt.audit,
            tracer=rt.tracer,
            sandbox=rt.sandbox,
            progress=self._progress,
            goal=self.goal,
        )
        offered = rt.chokepoint.available(self.ctx)
        self.tools = [t for t in offered if t["available"]]
        self.unavailable = [t for t in offered if not t["available"]]
        self.plan: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.evidence: dict[str, str] = {}
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "model_calls": 0,
                                      "queue_wait_ms": 0, "tool_calls": 0}
        self.steps = 0
        self.step_limit = rt.limits.max_steps
        self.started = time.monotonic()
        self.nudge = ""
        self.finish_refusals = 0
        self.deliverable_reminded = False
        self.failures_in_a_row = 0
        self.revision_from = -1  # history index where a revision began, if this run is one

    # -- the whole run ---------------------------------------------------------------------

    def run(self) -> str:
        with task_context(self.task_id, self.agent_id):
            try:
                self._restore()
                self.journal.append("claimed", {
                    "worker": self.worker_id, "agent": self.agent_id, "resumed": self.previous_status not in (None, "submitted"),
                    "tools": [t["name"] for t in self.tools],
                    "withheld_tools": [{"name": t["name"], "why": t["why_not"]} for t in self.unavailable],
                })
                if not self.plan:
                    self._make_plan()
                if self.previous_status == "revision_required":
                    self._begin_revision()
                set_status(self.rt.db, self.task_id, "running")
                return self._act()
            except TaskCancelled:
                return self._end("cancelled", error="cancelled on request")
            except BudgetExceeded as exc:
                if self.rt.audit is not None:
                    self.rt.audit.record("task.budget_exceeded", actor_id=self.user.user_id,
                                         payload={"task_id": self.task_id, "budget": exc.which, "used": exc.used, "limit": exc.limit})
                return self._end("failed", error=str(exc), budget={"which": exc.which, "used": exc.used, "limit": exc.limit})
            except NoEligibleModel as exc:
                return self._end("failed", error=f"no model is available for this step: {exc.decision.reason}")
            except (ProviderError, StructuredOutputError) as exc:
                return self._end("failed", error=f"the model runtime failed: {exc}")
            except Exception as exc:  # the loop must always leave the task in a terminal state
                return self._end("failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")

    # -- state ------------------------------------------------------------------------------

    def _restore(self) -> None:
        """Rebuild plan, history and evidence from the journal -- the resumption path."""
        plan = self.memory.get("plan")
        if isinstance(plan, dict):
            self.plan = plan
        pending: Optional[dict[str, Any]] = None
        for entry in self.journal.entries(limit=2000):
            kind, payload = entry["step_type"], entry["payload"] or {}
            if kind == "tool_call":
                pending = payload
            elif kind == "tool_result":
                self._remember(pending or {"tool": payload.get("tool"), "arguments": {}}, payload)
                pending = None
            elif kind == "model_call" and payload.get("purpose") == "act":
                self.steps += 1
                usage = payload.get("usage") or {}
                self.usage["prompt_tokens"] += int(usage.get("prompt") or 0)
                self.usage["completion_tokens"] += int(usage.get("completion") or 0)
                self.usage["model_calls"] += 1
            elif kind in ("planned", "replanned") and isinstance(payload.get("plan"), dict):
                self.plan = payload["plan"]

    def _remember(self, call: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        found: dict[str, str] = {}
        _harvest(result.get("for_model") or {}, found)
        self.evidence.update(found)
        self.history.append({
            "step": call.get("step"),
            "tool": result.get("tool") or call.get("tool"),
            "arguments": call.get("arguments") or {},
            "status": result.get("status"),
            "summary": result.get("summary"),
            "observation": result.get("for_model") or {},
        })

    def _history_lines(self) -> list[str]:
        lines = []
        cutoff = len(self.history) - RECENT_IN_FULL
        for index, item in enumerate(self.history):
            head = f"Step {index + 1}: {item['tool']} {json.dumps(item['arguments'], ensure_ascii=False)[:300]} -> {item['status']}: {item['summary']}"
            if index >= cutoff:
                body = json.dumps(item["observation"], ensure_ascii=False, default=str)
                if len(body) > OBSERVATION_CHARS:
                    body = body[:OBSERVATION_CHARS] + " ...[truncated]"
                lines.append(f"{head}\n  result: {body}")
            else:
                lines.append(head)
        return lines

    def _progress(self, event: Mapping[str, Any]) -> None:
        self.journal.append("progress", dict(event))

    def _checkpoint(self) -> None:
        if is_cancel_requested(self.rt.db, self.task_id):
            raise TaskCancelled()
        elapsed = int(time.monotonic() - self.started)
        limits = self.rt.limits
        if self.steps >= self.step_limit:
            raise BudgetExceeded("steps", self.steps, self.step_limit)
        tokens = self.usage["prompt_tokens"] + self.usage["completion_tokens"]
        if tokens >= limits.max_tokens:
            raise BudgetExceeded("tokens", tokens, limits.max_tokens)
        if elapsed >= limits.max_seconds:
            raise BudgetExceeded("wall clock seconds", elapsed, limits.max_seconds)
        heartbeat(self.rt.db, self.task_id)

    # -- models -----------------------------------------------------------------------------

    def _model(
        self,
        purpose: str,
        messages: Sequence[tuple[str, str]],
        schema: Mapping[str, Any],
        *,
        required: tuple[str, ...],
        preferred: Optional[str],
        max_tokens: int,
    ) -> Any:
        estimate = sum(len(content) for _, content in messages) // 3 + max_tokens

        def waiting(depth: int) -> None:
            self.journal.append("waiting", {"resource": "gpu", "queue_depth": depth, "purpose": purpose})

        result = self.rt.gateway.generate(
            RoutingRequest(
                purpose=purpose,
                required_capabilities=required,
                classification=self.classification,
                context_estimate=estimate,
                preferred_capability=preferred,
                task_id=self.task_id,
            ),
            [Message(role, content) for role, content in messages],
            schema=schema,
            temperature=0.1,
            max_tokens=max_tokens,
            actor_id=self.user.user_id,
            on_wait=waiting,
        )
        self.usage["prompt_tokens"] += result.usage.prompt_tokens
        self.usage["completion_tokens"] += result.usage.completion_tokens
        self.usage["model_calls"] += 1
        self.usage["queue_wait_ms"] += result.queue_wait_ms
        decision = result.routing
        self.journal.append("model_call", {
            "purpose": purpose,
            "step": self.steps if purpose == "act" else None,
            "model_id": result.model_id,
            "tag": result.tag,
            "reason": decision.reason,
            "preferred_capability": preferred,
            "candidates": [
                {"model_id": c.model_id, "eligible": c.eligible, "total": c.total, "summary": c.summary()}
                for c in decision.candidates
            ],
            "fallback_used": result.fallback_used,
            "degraded": result.degraded,
            "usage": {"prompt": result.usage.prompt_tokens, "completion": result.usage.completion_tokens},
            "latency_ms": result.latency_ms,
            "queue_wait_ms": result.queue_wait_ms,
        })
        return result

    # -- planning ---------------------------------------------------------------------------

    def _make_plan(self) -> None:
        self._checkpoint()
        guides = template_guide(self.ctx)
        messages = prompts.planner_messages(
            goal=self.goal,
            user={"username": self.user.username, "department": self.user.department},
            classification=self.classification,
            tools=self.tools,
            templates=guides,
        )
        result = self._model(
            "plan", messages, prompts.plan_schema([g["template_id"] for g in guides]),
            required=("planning", "structured_output"), preferred="planning", max_tokens=700,
        )
        plan = dict(result.data or {})
        plan["steps"] = [str(s)[:300] for s in plan.get("steps") or []][:12] or ["Work out what the goal needs."]
        if plan.get("deliverable") in (None, "", "none"):
            plan["deliverable"] = None
        if plan.get("primary_capability") not in ("reasoning", "code_generation"):
            plan["primary_capability"] = "reasoning"
        plan["model_id"] = result.model_id
        self._set_plan(plan, "planned")

    def _set_plan(self, plan: dict[str, Any], kind: str, reason: str = "") -> None:
        self.plan = plan
        self.memory.put("plan", plan)
        self.journal.append(kind, {"plan": plan, "reason": reason})
        set_status(self.rt.db, self.task_id, "running", plan=plan, primary_capability=plan.get("primary_capability"))

    def _begin_revision(self) -> None:
        request = self.memory.get("revision_request") or {}
        comment = str(request.get("comment") or "no comment given")
        self.ctx.revision_note = f"Revised after review: {comment[:90]}"
        self.step_limit = self.steps + self.rt.limits.revision_steps
        self.nudge = (
            f"The approver REJECTED the deliverable with this comment: \"{comment}\". Revise the content to "
            "address it -- search for more evidence if needed -- call the deliverable tool again, then finish."
        )
        self.deliverable_reminded = False
        self.revision_from = len(self.history)
        self.journal.append("revision", {"comment": comment, "revision": self.revision_count})

    # -- acting -----------------------------------------------------------------------------

    def _deliverable_tool(self) -> str:
        for tool in self.tools:
            if "template_id" in ((tool.get("schema") or {}).get("properties") or {}):
                return str(tool["name"])
        return ""

    def _act(self) -> str:
        tool_names = [t["name"] for t in self.tools]
        schema = prompts.action_schema(tool_names)
        deliverable = self.plan.get("deliverable")
        guide = ""
        if deliverable:
            match = next((g for g in template_guide(self.ctx) if g["template_id"] == deliverable), None)
            guide = prompts.describe_template(match) if match else ""
        deliverable_tool = self._deliverable_tool()
        repeats: Counter[str] = Counter()
        while True:
            self._checkpoint()
            self.steps += 1
            messages = prompts.actor_messages(
                goal=self.goal,
                user={"username": self.user.username, "department": self.user.department},
                classification=self.classification,
                plan=self.plan,
                tools=self.tools,
                template_guide=guide,
                deliverable_tool=deliverable_tool,
                evidence_index=list(self.evidence.values())[-40:],
                history=self._history_lines(),
                step=self.steps,
                max_steps=self.step_limit,
                nudge=self.nudge,
            )
            self.nudge = ""
            result = self._model(
                "act", messages, schema,
                required=("tool_calling", "structured_output"),
                preferred=self.plan.get("primary_capability") or "reasoning",
                max_tokens=2200,
            )
            action = result.data if isinstance(result.data, Mapping) else {}
            thought = str(action.get("thought") or "").strip()
            if thought:
                self.journal.append("thought", {"step": self.steps, "text": thought[:1500]})
            kind = action.get("action")
            if kind == "finish":
                outcome = self._finish(str(action.get("answer") or ""))
                if outcome is not None:
                    return outcome
                continue
            if kind == "replan":
                steps = [str(s)[:300] for s in action.get("new_plan") or [] if str(s).strip()]
                if steps:
                    self._set_plan({**self.plan, "steps": steps[:12]}, "replanned", reason=thought[:500])
                else:
                    self.nudge = "A replan needs new_plan: a list of steps."
                continue
            name = str(action.get("tool") or "")
            raw_arguments = action.get("arguments")
            arguments: dict[str, Any] = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
            key = json.dumps([name, arguments], sort_keys=True, default=str)
            repeats[key] += 1
            if repeats[key] > 2:
                self.nudge = "You have already made this exact call twice. Use its result, change the arguments, or finish."
                continue
            self._call(name, arguments)

    def _call(self, name: str, arguments: dict[str, Any]) -> None:
        call = {"step": self.steps, "tool": name, "arguments": _truncate(arguments, 6000)}
        self.journal.append("tool_call", call)
        result = self.rt.chokepoint.invoke(self.ctx, name, arguments)
        self.usage["tool_calls"] += 1
        record = result.to_dict()
        record["output"] = _truncate(record["output"], 12000)
        record["detail"] = _truncate(record["detail"], 30000)
        record["for_model"] = _truncate(result.for_model(), 9000)
        record["step"] = self.steps
        self.journal.append("tool_result", record)
        self._remember(call, record)
        if result.ok:
            self.failures_in_a_row = 0
        else:
            self.failures_in_a_row += 1
            if self.failures_in_a_row >= 3:
                self.nudge = "Your last three actions did not succeed. Consider replanning (action \"replan\") or finishing with what you have."

    def _finish(self, answer: str) -> Optional[str]:
        answer = answer.strip()
        if not answer:
            self.nudge = "To finish, give an answer: the result, with evidence ids in square brackets."
            return None
        unknown = [c for c in cited(answer) if c not in self.evidence]
        if unknown and self.finish_refusals < 2:
            self.finish_refusals += 1
            known = ", ".join(sorted(self.evidence)) or "none"
            self.nudge = f"Your answer cites {', '.join(unknown)}, which you were never given. Cite only ids you hold ({known})."
            return None
        deliverable = self.plan.get("deliverable")
        if deliverable and not self.deliverable_reminded and self._deliverable_tool() and self.steps < self.step_limit - 1:
            verified = self.rt.db.scalar(
                "SELECT count(*) FROM artifacts WHERE task_id = %(t)s::uuid AND template_id = %(k)s "
                "AND status IN ('VERIFIED', 'APPROVED', 'RELEASED') AND created_at > now() - interval '1 day'",
                {"t": self.task_id, "k": deliverable},
            )
            if not verified or (self.revision_from >= 0 and not self._generated_this_run()):
                self.deliverable_reminded = True
                self.nudge = (f"This task must produce the {deliverable} deliverable and there is no verified one yet. "
                              f"Call {self._deliverable_tool()} before finishing.")
                return None
        return self._complete(answer)

    def _generated_this_run(self) -> bool:
        return any(item.get("tool") == self._deliverable_tool() and item.get("status") == "ok"
                   for item in self.history[self.revision_from:])

    def _complete(self, answer: str) -> str:
        artifacts = self.rt.db.query(
            "SELECT id::text AS id, title, template_id, filename, status, requires_approval, version, kind "
            "FROM artifacts WHERE task_id = %(t)s::uuid ORDER BY created_at",
            {"t": self.task_id},
        )
        pending = [
            a for a in artifacts
            if a["requires_approval"] and a["status"] == "VERIFIED"
            and not self.rt.db.scalar("SELECT count(*) FROM approvals WHERE artifact_id = %(a)s::uuid", {"a": a["id"]})
        ]
        status = "awaiting_approval" if pending else "completed"
        result = {
            "answer": answer,
            "citations": [c for c in cited(answer) if c in self.evidence],
            "artifacts": artifacts,
            "awaiting_approval": [a["id"] for a in pending],
            "evidence": list(self.evidence.values()),
        }
        return self._end(status, result=result)

    def _end(self, status: str, *, error: Optional[str] = None, result: Optional[dict[str, Any]] = None,
             budget: Optional[dict[str, Any]] = None) -> str:
        elapsed = int(time.monotonic() - self.started)
        usage = {**self.usage, "steps": self.steps, "elapsed_s": elapsed}
        fields: dict[str, Any] = {"usage": usage}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error"] = error
            if result is None:
                fields["result"] = {"answer": None, "evidence": list(self.evidence.values()), "error": error}
        set_status(self.rt.db, self.task_id, status, **fields)
        payload: dict[str, Any] = {"status": status, "usage": usage}
        if error:
            payload["error"] = error
        if budget:
            payload["budget"] = budget
        if result is not None:
            payload["answer"] = result.get("answer")
            payload["artifacts"] = result.get("artifacts")
            payload["awaiting_approval"] = result.get("awaiting_approval")
        self.journal.append({"completed": "finished", "awaiting_approval": "finished"}.get(status, status), payload)
        if status == "cancelled" and self.rt.audit is not None:
            self.rt.audit.record("task.cancelled", actor_id=self.user.user_id, payload={"task_id": self.task_id, "while": "running"})
        return status


def run_task(rt: Runtime, task: Mapping[str, Any], worker_id: str) -> str:
    return Agent(rt, task, worker_id).run()


def refresh(rt: Runtime, task_id: str) -> Optional[dict[str, Any]]:
    return get_task(rt.db, task_id)


__all__ = ["Runtime", "Agent", "BudgetLimits", "BudgetExceeded", "TaskCancelled", "run_task", "cited"]
