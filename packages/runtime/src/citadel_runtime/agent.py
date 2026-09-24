"""The agent runtime: one task, one or more agents, one shared state, one journal.

    recall what the workbench remembers -> plan (the real goal, grounded in that memory)
      -> [helper agents, if the plan split the work: each works its part -- in parallel
          where nothing depends on anything, dependencies first -- and leaves its
          findings, with evidence ids, in the task's shared state]
      -> the lead acts with everyone's findings: plan -> call a tool through the
         chokepoint -> observe -> continue | replan | finish
      -> the memory manager records what the task established

Every agent is the same loop. Each action is one JSON object, constrained to the schema
of the tools the policy offers this actor for this task. Observations are handled by
their shape -- anything carrying an `evidence_id` joins the evidence the agent may cite
-- never by which tool produced them, so a new tool needs no change here.

People work here too: what a person does in the workbench (runs a tool, adds a fact, a
decision, an assumption, a question, a note steering one agent) is journalled like an
agent's step and is in front of every agent at its next turn.

Budgets (steps per agent; tokens and wall clock per task) and cancellation and pause
are checked before every model call by every agent. Every step is journalled -- tagged
with the agent that took it -- before the next begins; a worker that dies mid-task, or
a task that is paused, leaves a journal another claim resumes from, agent by agent.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from citadel_contracts.domain import User
from citadel_gateway import Message, NoEligibleModel, ProviderError, RoutingRequest, StructuredOutputError
from citadel_memory import MemoryManager, WorkingMemory, remember_task
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.identity.store import get_user_by_external_identity
from citadel_platform.registry import Registry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer, task_context
from citadel_tools import Chokepoint, DataBoundary, SandboxRunner, ToolContext, actor_facts_for_task
from citadel_tools.doc import template_guide

from citadel_runtime import prompts
from citadel_runtime.tasks import LATEST_VERSION, Journal, get_task, heartbeat, interruption, latest_deliverable, set_status

_CITE = re.compile(r"\b([EC]\d+)\b")
_CITE_GROUP = re.compile(r"\[((?:[EC]\d+)(?:\s*[,;]\s*[EC]\d+)*)\]")
RECENT_IN_FULL = 4
OBSERVATION_CHARS = 2600
LEAD = "lead"


@dataclass(frozen=True)
class BudgetLimits:
    max_steps: int = 14
    max_tokens: int = 150_000
    max_seconds: int = 1500
    revision_steps: int = 8
    agent_steps: int = 6
    ask_steps: int = 6


class TaskCancelled(Exception):
    pass


class TaskPaused(Exception):
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
    remember: bool = True  # the memory manager records what finished tasks established

    def user(self, external_identity: str) -> Optional[User]:
        if self.lookup_user is not None:
            return self.lookup_user(external_identity)
        return get_user_by_external_identity(self.db.env, external_identity)


class Usage:
    """Task-wide counters, shared by every agent of the task (they run on threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.values: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "model_calls": 0,
                                       "queue_wait_ms": 0, "tool_calls": 0}

    def add(self, **amounts: int) -> None:
        with self._lock:
            for key, amount in amounts.items():
                self.values[key] = self.values.get(key, 0) + int(amount)

    def tokens(self) -> int:
        with self._lock:
            return self.values["prompt_tokens"] + self.values["completion_tokens"]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.values)


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


def _harvest_changes(obj: Any, found: dict[str, str], _texts: Optional[dict[str, list[str]]] = None) -> None:
    """A version diff cites old and new wording by id without an `evidence_id` key. One
    passage often holds several changed sentences; its line gathers all of them, so the
    id can be cited for any of the changes it carries."""
    top = _texts is None
    texts: dict[str, list[str]] = {} if _texts is None else _texts
    if isinstance(obj, Mapping):
        for key, label in (("before_evidence", "earlier wording"), ("after_evidence", "later wording")):
            ids = obj.get(key)
            text = obj.get("before" if key == "before_evidence" else "after")
            if isinstance(ids, list) and text:
                for eid in ids:
                    if isinstance(eid, str) and _CITE.fullmatch(eid):
                        texts.setdefault(f"{eid}\x00{label}", []).append(" ".join(str(text).split()))
        for value in obj.values():
            _harvest_changes(value, found, texts)
    elif isinstance(obj, list):
        for value in obj:
            _harvest_changes(value, found, texts)
    if top:
        for key, parts in texts.items():
            eid, label = key.split("\x00", 1)
            if eid not in found:
                found[eid] = f"{eid} ({label}): {' | '.join(parts)}"[:420]


def cited(text: str) -> list[str]:
    ids: list[str] = []
    for group in _CITE_GROUP.findall(text or ""):
        for eid in _CITE.findall(group):
            if eid not in ids:
                ids.append(eid)
    return ids


class AgentLoop:
    """One agent's plan-act-observe loop. The lead and every helper agent run this."""

    def __init__(
        self,
        run: "TaskRun",
        *,
        agent_id: str,
        name: str,
        goal: str,
        plan: dict[str, Any],
        role: Optional[dict[str, Any]],
        step_limit: int,
        allow_deliverable: bool,
        mode: str,
        team: bool,
    ) -> None:
        self.run = run
        self.rt = run.rt
        self.agent_id = agent_id
        self.name = name
        self.goal = goal
        self.plan = plan
        self.role = role
        self.mode = mode
        self.journal = Journal(run.rt.db, run.task_id, agent_id=agent_id)
        self.ctx = run.make_context(agent_id)
        self.tools = run.tools_for(mode=mode, team=team, allow_deliverable=allow_deliverable)
        self.allow_deliverable = allow_deliverable
        self.history: list[dict[str, Any]] = []
        self.evidence: dict[str, str] = {}
        self.team_findings: list[str] = []
        self.steps = 0
        self.step_limit = step_limit
        self.nudge = ""
        self.finish_refusals = 0
        self.deliverable_reminded = False
        self.failures_in_a_row = 0
        self.revision_from = -1  # history index where a revision began, if this run is one
        self.seen_seq = 0  # journal watermark for what people did
        self.delivered_notes: set[int] = set()
        self.brief = ""  # a standing instruction in every prompt of this run (a revision request)

    # -- state ------------------------------------------------------------------------------

    def restore(self, *, include_legacy: bool = False) -> None:
        """Rebuild this agent's history and evidence from its own journal entries."""
        ids: list[Optional[str]] = [self.agent_id] + ([None] if include_legacy else [])
        pending: Optional[dict[str, Any]] = None
        for entry in self.journal.entries_of(ids):
            kind, payload = entry["step_type"], entry["payload"] or {}
            if kind == "tool_call":
                pending = payload
            elif kind == "tool_result":
                self.remember(pending or {"tool": payload.get("tool"), "arguments": {}}, payload)
                pending = None
            elif kind == "model_call" and payload.get("purpose") == "act":
                self.steps += 1
                usage = payload.get("usage") or {}
                self.run.usage.add(prompt_tokens=int(usage.get("prompt") or 0),
                                   completion_tokens=int(usage.get("completion") or 0), model_calls=1)
            elif kind in ("planned", "replanned") and isinstance(payload.get("plan"), dict) and self.agent_id == LEAD:
                self.plan = payload["plan"]
            elif kind == "steered" and payload.get("note_id") is not None:
                self.delivered_notes.add(int(payload["note_id"]))
        for entry in self.journal.human_steps():
            self.seen_seq = max(self.seen_seq, int(entry["step_seq"]))
            if entry["step_type"] == "tool_result":
                self.remember({"tool": (entry["payload"] or {}).get("tool"), "arguments": {}}, entry["payload"] or {})

    def remember(self, call: Mapping[str, Any], result: Mapping[str, Any], *, by: Optional[str] = None) -> None:
        found: dict[str, str] = {}
        observation = result.get("for_model") or {}
        _harvest(observation, found)
        _harvest_changes(observation, found)
        self.evidence.update(found)
        self.history.append({
            "step": call.get("step"),
            "tool": result.get("tool") or call.get("tool"),
            "arguments": call.get("arguments") or {},
            "status": result.get("status"),
            "summary": result.get("summary"),
            "observation": observation,
            "by": by,
        })

    def history_lines(self) -> list[str]:
        lines = []
        cutoff = len(self.history) - RECENT_IN_FULL
        for index, item in enumerate(self.history):
            who = f" (done by {item['by']})" if item.get("by") else ""
            head = (f"Step {index + 1}: {item['tool']} {json.dumps(item['arguments'], ensure_ascii=False)[:300]} -> "
                    f"{item['status']}: {item['summary']}{who}")
            if index >= cutoff:
                body = json.dumps(item["observation"], ensure_ascii=False, default=str)
                if len(body) > OBSERVATION_CHARS:
                    body = body[:OBSERVATION_CHARS] + " ...[truncated]"
                lines.append(f"{head}\n  result: {body}")
            else:
                lines.append(head)
        return lines

    def absorb_people(self) -> None:
        """What people did since this agent's last turn: tool results they obtained join
        its history and evidence; a note addressed to this agent becomes its next NOTE."""
        for entry in self.journal.human_steps(after=self.seen_seq):
            self.seen_seq = max(self.seen_seq, int(entry["step_seq"]))
            payload = entry["payload"] or {}
            if entry["step_type"] == "tool_result":
                self.remember({"tool": payload.get("tool"), "arguments": payload.get("arguments") or {}}, payload,
                              by=str(payload.get("by_name") or entry.get("agent_id")))
        for note in self.run.notes_for(self.agent_id):
            if int(note["id"]) in self.delivered_notes:
                continue
            self.delivered_notes.add(int(note["id"]))
            who = str(note["author"]).removeprefix("human:")
            self.nudge = (self.nudge + " " if self.nudge else "") + f"{who} says to you: \"{note['content']}\""
            # journalled, so a resumed agent is not told the same thing twice
            self.journal.append("steered", {"note_id": int(note["id"]), "by": note["author"],
                                            "text": str(note["content"])[:500]})

    # -- the loop ---------------------------------------------------------------------------

    def _deliverable_tool(self) -> str:
        for tool in self.tools:
            if "template_id" in ((tool.get("schema") or {}).get("properties") or {}):
                return str(tool["name"])
        return ""

    def act(self) -> str:
        """Run until the agent finishes; returns its final answer (the lead's) or its
        findings (a helper's)."""
        tool_names = [t["name"] for t in self.tools]
        schema = prompts.action_schema(tool_names)
        deliverable = self.plan.get("deliverable") if self.allow_deliverable else None
        guide = ""
        if deliverable:
            match = next((g for g in template_guide(self.ctx) if g["template_id"] == deliverable), None)
            guide = prompts.describe_template(match) if match else ""
        deliverable_tool = self._deliverable_tool() if deliverable else ""
        repeats: Counter[str] = Counter()
        while True:
            self.run.checkpoint(self)
            self.absorb_people()
            self.steps += 1
            self.run.set_agent(self.agent_id, current_step=f"step {self.steps} of {self.step_limit}")
            messages = prompts.actor_messages(
                goal=self.goal,
                user={"username": self.run.user.username, "department": self.run.user.department},
                classification=self.run.classification,
                plan=self.plan,
                tools=self.tools,
                template_guide=guide,
                deliverable_tool=deliverable_tool,
                evidence_index=list(self.evidence.values())[-40:],
                history=self.history_lines(),
                step=self.steps,
                max_steps=self.step_limit,
                nudge=self.nudge,
                role=self.role,
                team_findings=self.team_findings,
                shared_state=self.run.shared_lines(),
                memories=self.run.memories,
                mode=self.mode,
                brief=self.brief,
            )
            self.nudge = ""
            result = self.run.model(
                self, "act", messages, schema,
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
                outcome = self.finish(str(action.get("answer") or ""))
                if outcome is not None:
                    return outcome
                continue
            if kind == "replan":
                steps = [str(s)[:300] for s in action.get("new_plan") or [] if str(s).strip()]
                if steps:
                    self.plan = {**self.plan, "steps": steps[:12]}
                    self.run.replanned(self, thought)
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
            self.call(name, arguments)

    def call(self, name: str, arguments: dict[str, Any]) -> None:
        call = {"step": self.steps, "tool": name, "arguments": _truncate(arguments, 6000)}
        self.journal.append("tool_call", call)
        self.run.set_agent(self.agent_id, current_step=f"using {name}")
        result = self.rt.chokepoint.invoke(self.ctx, name, arguments)
        self.run.usage.add(tool_calls=1)
        record = result.to_dict()
        record["output"] = _truncate(record["output"], 12000)
        record["detail"] = _truncate(record["detail"], 30000)
        record["for_model"] = _truncate(result.for_model(), 9000)
        record["step"] = self.steps
        record["arguments"] = call["arguments"]
        self.journal.append("tool_result", record)
        self.remember(call, record)
        if result.ok:
            self.failures_in_a_row = 0
        else:
            self.failures_in_a_row += 1
            if self.failures_in_a_row >= 3:
                self.nudge = "Your last three actions did not succeed. Consider replanning (action \"replan\") or finishing with what you have."

    def finish(self, answer: str) -> Optional[str]:
        answer = answer.strip()
        if not answer:
            self.nudge = "To finish, give an answer: the result, with evidence ids in square brackets."
            return None
        unknown = [c for c in cited(answer) if c not in self.evidence]
        if unknown and self.finish_refusals < 2:
            self.finish_refusals += 1
            known = ", ".join(sorted(self.evidence, key=lambda e: (e[0], int(e[1:])))) or "none"
            self.nudge = f"Your answer cites {', '.join(unknown)}, which you were never given. Cite only ids you hold ({known})."
            return None
        deliverable = self.plan.get("deliverable") if self.allow_deliverable else None
        if deliverable and not self.deliverable_reminded and self._deliverable_tool() and self.steps < self.step_limit - 1:
            verified = self.rt.db.scalar(
                "SELECT count(*) FROM artifacts WHERE task_id = %(t)s::uuid AND template_id = %(k)s "
                "AND status IN ('VERIFIED', 'APPROVED', 'RELEASED') AND created_at > now() - interval '1 day'",
                {"t": self.run.task_id, "k": deliverable},
            )
            if not verified or (self.revision_from >= 0 and not self._generated_this_run()):
                self.deliverable_reminded = True
                self.nudge = (f"This task must produce the {deliverable} deliverable and there is no verified one yet. "
                              f"Call {self._deliverable_tool()} before finishing.")
                return None
        return answer

    def _generated_this_run(self) -> bool:
        return any(item.get("tool") == self._deliverable_tool() and item.get("status") == "ok"
                   for item in self.history[self.revision_from:])


class TaskRun:
    """One claim of one task: restore, recall, plan (perhaps splitting the work across
    agents), run the agents, let the lead finish, record what was learned."""

    def __init__(self, rt: Runtime, task: Mapping[str, Any], worker_id: str) -> None:
        self.rt = rt
        self.task = dict(task)
        self.task_id = str(task["id"])
        self.goal = str(task["goal"])
        self.kind = str(task.get("kind") or "task")
        self.classification = str(task["classification"]).upper()
        self.previous_status = task.get("previous_status")
        self.revision_count = int(task.get("revision_count") or 0)
        self.worker_id = worker_id
        user = rt.user(str(task["submitted_by"]))
        if user is None:
            raise RuntimeError(f"task {self.task_id} was submitted by an identity that no longer exists")
        self.user = user
        self.journal = Journal(rt.db, self.task_id)
        self.working = WorkingMemory(rt.db, self.task_id)
        self.usage = Usage()
        self.started = time.monotonic()
        self.offered = rt.chokepoint.available(self.make_context(LEAD))
        self.plan: dict[str, Any] = {}
        self.memories: list[str] = []
        self.members: list[AgentLoop] = []
        self._agent_lock = threading.Lock()
        limits = rt.limits
        self.lead = AgentLoop(
            self, agent_id=LEAD, name="Lead agent", goal=self.goal, plan={}, role=None,
            step_limit=limits.ask_steps if self.kind == "ask" else limits.max_steps,
            allow_deliverable=self.kind == "task", mode="ask" if self.kind == "ask" else "task", team=False,
        )

    # -- plumbing shared by every agent ------------------------------------------------------

    def make_context(self, agent_id: str) -> ToolContext:
        journal = Journal(self.rt.db, self.task_id, agent_id=agent_id)

        def progress(event: Mapping[str, Any]) -> None:
            journal.append("progress", dict(event))

        return ToolContext(
            task_id=self.task_id,
            agent_id=f"{agent_id}-{self.task_id[:8]}",
            user=self.user,
            actor=actor_facts_for_task(self.user, self.rt.registry, self.classification),
            task_classification=self.classification,
            db=self.rt.db,
            data_dir=self.rt.data_dir,
            registry=self.rt.registry,
            registry_dir=self.rt.registry_dir,
            boundary=self.rt.boundary,
            gateway=self.rt.gateway,
            audit=self.rt.audit,
            tracer=self.rt.tracer,
            sandbox=self.rt.sandbox,
            progress=progress,
            goal=self.goal,
        )

    def tools_for(self, *, mode: str, team: bool, allow_deliverable: bool) -> list[dict[str, Any]]:
        """Which offered tools an agent sees. Decided by data -- the policy's verdict and
        each tool's registry options -- never by a tool's name."""
        tools = []
        for tool in self.offered:
            if not tool["available"]:
                continue
            offered_in = (tool.get("options") or {}).get("offered_in")
            writes_deliverable = "template_id" in ((tool.get("schema") or {}).get("properties") or {})
            if mode == "ask":
                if tool.get("side_effect") != "read" or (offered_in and "ask" not in offered_in):
                    continue
            else:
                if offered_in and not ("task" in offered_in or (team and "team" in offered_in)):
                    continue
                if writes_deliverable and not allow_deliverable:
                    continue
            tools.append(tool)
        return tools

    def checkpoint(self, loop: AgentLoop) -> None:
        state = interruption(self.rt.db, self.task_id)
        if state == "cancel":
            raise TaskCancelled()
        if state == "pause":
            raise TaskPaused()
        limits = self.rt.limits
        if loop.steps >= loop.step_limit:
            raise BudgetExceeded("steps", loop.steps, loop.step_limit)
        tokens = self.usage.tokens()
        if tokens >= limits.max_tokens:
            raise BudgetExceeded("tokens", tokens, limits.max_tokens)
        elapsed = int(time.monotonic() - self.started)
        if elapsed >= limits.max_seconds:
            raise BudgetExceeded("wall clock seconds", elapsed, limits.max_seconds)
        heartbeat(self.rt.db, self.task_id)

    def model(
        self,
        loop: AgentLoop,
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
            loop.journal.append("waiting", {"resource": "gpu", "queue_depth": depth, "purpose": purpose})

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
        self.journal_model_call(loop.journal, purpose, result, step=loop.steps if purpose == "act" else None,
                                preferred=preferred)
        return result

    def journal_model_call(self, journal: Journal, purpose: str, result: Any, *, step: Optional[int] = None,
                           preferred: Optional[str] = None) -> None:
        self.usage.add(prompt_tokens=result.usage.prompt_tokens, completion_tokens=result.usage.completion_tokens,
                       model_calls=1, queue_wait_ms=result.queue_wait_ms)
        decision = result.routing
        journal.append("model_call", {
            "purpose": purpose,
            "step": step,
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

    def set_agent(self, agent_id: str, **fields: Any) -> None:
        if agent_id == LEAD and not self.members:
            return
        assignments = ", ".join(f"{key} = %({key})s" for key in fields)
        with self._agent_lock:
            self.rt.db.execute(
                f"UPDATE task_agents SET {assignments} WHERE task_id = %(t)s::uuid AND agent_id = %(a)s",
                {**fields, "t": self.task_id, "a": agent_id},
            )

    def shared_lines(self) -> list[str]:
        rows = self.rt.db.query(
            "SELECT id, kind, content, evidence, author, addressed_to FROM task_shared_state "
            "WHERE task_id = %(t)s::uuid AND kind <> 'plan' ORDER BY id DESC LIMIT 16",
            {"t": self.task_id},
        )
        lines = []
        for row in reversed(rows):
            who = str(row["author"])
            who = f"person {who.removeprefix('human:')}" if who.startswith("human:") else self._agent_name(who)
            cites = "".join(f" [{e}]" for e in row["evidence"] or [])
            to = f" (to {self._agent_name(str(row['addressed_to']))})" if row.get("addressed_to") else ""
            lines.append(f"- {row['kind']} by {who}{to}: {' '.join(str(row['content']).split())[:500]}{cites}")
        return lines

    def notes_for(self, agent_id: str) -> list[dict[str, Any]]:
        return self.rt.db.query(
            "SELECT id, content, author FROM task_shared_state WHERE task_id = %(t)s::uuid AND addressed_to = %(a)s "
            "ORDER BY id",
            {"t": self.task_id, "a": agent_id},
        )

    def _agent_name(self, agent_id: str) -> str:
        if agent_id.startswith(LEAD):
            return "the lead agent"
        for member in self.members:
            if agent_id.startswith(member.agent_id):
                return member.name
        return agent_id

    def note(self, kind: str, content: str, *, author: str, evidence: Sequence[str] = (), agent_id: Optional[str] = None) -> None:
        self.rt.db.execute(
            "INSERT INTO task_shared_state (task_id, kind, content, evidence, agent_id, author) VALUES "
            "(%(t)s::uuid, %(k)s, %(c)s, %(e)s, %(a)s, %(by)s)",
            {"t": self.task_id, "k": kind, "c": content[:4000], "e": list(evidence), "a": agent_id, "by": author},
        )

    def replanned(self, loop: AgentLoop, reason: str) -> None:
        loop.journal.append("replanned", {"plan": loop.plan, "reason": reason[:500]})
        if loop.agent_id == LEAD:
            self.plan = loop.plan
            self.working.put("plan", self.plan)
            set_status(self.rt.db, self.task_id, "running", plan=self.plan)
        else:
            self.set_agent(loop.agent_id, plan=_json(loop.plan))

    # -- the whole run -------------------------------------------------------------------------

    def run(self) -> str:
        with task_context(self.task_id, f"{LEAD}-{self.task_id[:8]}"):
            try:
                self._restore()
                self.journal.append("claimed", {
                    "worker": self.worker_id, "agent": f"{LEAD}-{self.task_id[:8]}", "kind": self.kind,
                    "resumed": self.previous_status not in (None, "submitted"),
                    "tools": [t["name"] for t in self.lead.tools],
                    "withheld_tools": [{"name": t["name"], "why": t["why_not"]} for t in self.offered if not t["available"]],
                })
                if self.previous_status == "paused":
                    self.journal.append("resumed", {"by": "worker", "worker": self.worker_id})
                if not self.plan:
                    if self.kind == "ask":
                        self._set_plan({"understanding": self.goal[:300], "steps": ["Find the answer", "Answer briefly, citing sources"],
                                        "primary_capability": "reasoning", "deliverable": None}, "planned")
                    else:
                        self._recall()
                        self._make_plan()
                self.lead.plan = self.plan
                if self.previous_status == "revision_required":
                    self._begin_revision()
                set_status(self.rt.db, self.task_id, "running")
                self._run_team()
                answer = self.lead.act()
                return self._complete(answer)
            except TaskPaused:
                return self._pause()
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

    def _restore(self) -> None:
        """Rebuild the plan, the agents and everyone's history from the journal -- the
        resumption path after a worker died or a person paused the task."""
        plan = self.working.get("plan")
        if isinstance(plan, dict):
            self.plan = plan
        memories = self.working.get("memories")
        if isinstance(memories, list):
            self.memories = [str(m) for m in memories]
        brief = self.working.get("revision_brief")
        if isinstance(brief, str) and self.previous_status not in (None, "submitted", "revision_required"):
            self.lead.brief = brief
        self.lead.restore(include_legacy=True)
        if self.lead.plan and not self.plan:
            self.plan = self.lead.plan

    # -- grounding and planning ------------------------------------------------------------

    def _recall(self) -> None:
        """Ground the plan in what the workbench remembers -- through the chokepoint, as a
        recall made on the lead agent's behalf, so policy and audit see it like any read."""
        if not any(t["name"] == "memory.recall" and t["available"] for t in self.offered):
            return
        result = self.rt.chokepoint.invoke(self.lead.ctx, "memory.recall", {"query": self.goal[:400], "top_k": 5})
        memories = (result.output or {}).get("memories") if result.ok else None
        lines = []
        for memory in memories or []:
            lines.append(f"{memory.get('ref')} ({memory.get('type')}, {memory.get('remembered_on')}): {memory.get('content')}")
        self.memories = lines
        self.working.put("memories", lines)
        self.lead.journal.append("recalled", {
            "status": result.status, "count": len(lines), "memories": (result.detail or {}).get("memories") or [],
            "summary": result.summary,
        })

    def _make_plan(self) -> None:
        self.checkpoint(self.lead)
        guides = template_guide(self.lead.ctx)
        team_tools = self.tools_for(mode="task", team=True, allow_deliverable=True)
        messages = prompts.planner_messages(
            goal=self.goal,
            user={"username": self.user.username, "department": self.user.department},
            classification=self.classification,
            tools=team_tools,
            templates=guides,
            memories=self.memories,
            team=True,
        )
        result = self.model(
            self.lead, "plan", messages, prompts.plan_schema([g["template_id"] for g in guides]),
            required=("planning", "structured_output"), preferred="planning", max_tokens=900,
        )
        plan = dict(result.data or {})
        plan["steps"] = [str(s)[:300] for s in plan.get("steps") or []][:12] or ["Work out what the goal needs."]
        if plan.get("deliverable") in (None, "", "none"):
            plan["deliverable"] = None
        if plan.get("primary_capability") not in ("reasoning", "code_generation"):
            plan["primary_capability"] = "reasoning"
        plan["assumptions"] = [str(a)[:300] for a in plan.get("assumptions") or [] if str(a).strip()][:5]
        plan["agents"] = self._clean_agents(plan.get("agents"), {t["name"] for t in team_tools})
        plan["model_id"] = result.model_id
        self._set_plan(plan, "planned")
        for assumption in plan["assumptions"]:
            self.note("assumption", assumption, author=f"{LEAD}-{self.task_id[:8]}", agent_id=LEAD)

    @staticmethod
    def _clean_agents(raw: Any, tool_names: set[str]) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        for index, spec in enumerate(raw if isinstance(raw, list) else [], start=1):
            if not isinstance(spec, Mapping) or not str(spec.get("goal") or "").strip():
                continue
            agents.append({
                "number": len(agents) + 1,
                "name": f"Agent {len(agents) + 1}",
                "goal": " ".join(str(spec["goal"]).split())[:400],
                "focus": [str(t) for t in spec.get("focus") or [] if str(t) in tool_names][:4],
                "depends_on": [int(d) for d in spec.get("depends_on") or []
                               if isinstance(d, (int, float)) and 1 <= int(d) <= len(raw) and int(d) != index],
            })
            if len(agents) >= prompts.MAX_AGENTS:
                break
        return agents

    def _set_plan(self, plan: dict[str, Any], kind: str, reason: str = "") -> None:
        self.plan = plan
        self.lead.plan = plan
        self.working.put("plan", plan)
        self.lead.journal.append(kind, {"plan": plan, "reason": reason})
        set_status(self.rt.db, self.task_id, "running", plan=plan, primary_capability=plan.get("primary_capability"))

    def _begin_revision(self) -> None:
        """Back for another version: after an approver's rejection, or because the person
        who asked for it wants something changed. Either way the lead starts from the
        deliverable's CURRENT version -- which may carry the person's own edits -- not
        from what it generated last."""
        request = self.working.get("revision_request") or {}
        comment = str(request.get("comment") or "no comment given")
        owner = request.get("kind") == "owner"
        name = str(request.get("name") or "the person who asked for it")
        latest = latest_deliverable(self.rt.db, self.task_id)
        current = ""
        if latest is not None and latest.get("content"):
            edited = ""
            if str(latest.get("note") or "").startswith("Edited by"):
                edited = f", {latest['note'][:80]}"
            body = json.dumps(latest["content"], ensure_ascii=False)
            current = (f"\nCURRENT VERSION (v{latest['version']} of {latest['template_id']}{edited}) -- revise THIS, "
                       f"keeping what is not asked to change:\n{body[:7000]}")
            if latest.get("template_id"):
                self.plan = {**self.plan, "deliverable": latest["template_id"]}
                self.lead.plan = self.plan
        self.lead.step_limit = self.lead.steps + self.rt.limits.revision_steps
        if owner:
            self.lead.ctx.revision_note = f"Revised at {name}'s request: {comment[:80]}"
            self.lead.nudge = (
                f"{name}, who asked for this deliverable, wants it revised: \"{comment}\". Change what they ask -- "
                "search for more evidence if the change needs it -- then call the deliverable tool again with the "
                "whole revised content, and finish."
            )
            self.lead.brief = f"{name} asked: \"{comment}\"{current}"
        else:
            self.lead.ctx.revision_note = f"Revised after review: {comment[:90]}"
            self.lead.nudge = (
                f"The approver REJECTED the deliverable with this comment: \"{comment}\". Revise the content to "
                "address it -- search for more evidence if needed -- call the deliverable tool again, then finish."
            )
            self.lead.brief = f"The approver rejected it: \"{comment}\"{current}"
        self.working.put("revision_brief", self.lead.brief)  # a worker that dies mid-revision resumes with it
        self.lead.deliverable_reminded = False
        self.lead.revision_from = len(self.lead.history)
        self.lead.journal.append("revision", {"comment": comment, "revision": self.revision_count,
                                              "kind": "owner" if owner else "approver", "by": request.get("by"),
                                              "from_version": latest["version"] if latest else None})
        self.note("decision", (f"Revise the deliverable at {name}'s request: {comment}" if owner
                               else f"Revise the deliverable after review: {comment}"),
                  author=f"{LEAD}-{self.task_id[:8]}", agent_id=LEAD)

    # -- the team -------------------------------------------------------------------------------

    def _run_team(self) -> None:
        specs = [a for a in self.plan.get("agents") or [] if isinstance(a, Mapping)]
        if not specs or self.kind != "task":
            return
        by_number = {int(a["number"]): f"agent_{int(a['number'])}" for a in specs}
        rows = {str(r["agent_id"]): r for r in self.rt.db.query(
            "SELECT agent_id, status, findings FROM task_agents WHERE task_id = %(t)s::uuid", {"t": self.task_id}
        )}
        if not rows:
            self._spawn(specs, by_number)
            rows = {f"agent_{int(a['number'])}": {"status": "pending", "findings": None} for a in specs}
        self.lead.tools = self.tools_for(mode="task", team=True, allow_deliverable=True)
        for spec in specs:
            agent_id = by_number[int(spec["number"])]
            member = AgentLoop(
                self, agent_id=agent_id, name=str(spec["name"]), goal=str(spec["goal"]),
                plan={"steps": [str(spec["goal"])], "primary_capability": self.plan.get("primary_capability"),
                      "deliverable": None},
                role={"name": spec["name"], "team_goal": self.goal},
                step_limit=self.rt.limits.agent_steps, allow_deliverable=False, mode="task", team=True,
            )
            if spec.get("focus"):
                focused = [t for t in member.tools if t["name"] in spec["focus"] or t["name"] == "state.note"]
                member.tools = focused or member.tools
            member.restore()
            self.members.append(member)
        depends = {by_number[int(s["number"])]: [by_number[d] for d in s.get("depends_on") or [] if d in by_number]
                   for s in specs}
        done = {aid for aid, row in rows.items() if row.get("status") in ("done", "failed")}
        remaining = [m for m in self.members if m.agent_id not in done]
        interrupted: Optional[BaseException] = None
        while remaining and interrupted is None:
            ready = [m for m in remaining if all(d in done for d in depends.get(m.agent_id, []))]
            if not ready:  # a dependency cycle: say so and run them anyway
                self.note("decision", "The agents' dependencies form a cycle; running them without waiting.",
                          author=f"{LEAD}-{self.task_id[:8]}", agent_id=LEAD)
                ready = list(remaining)
            for member in remaining:
                if member not in ready:
                    waits = [self._agent_name(d) for d in depends.get(member.agent_id, []) if d not in done]
                    self.set_agent(member.agent_id, status="waiting",
                                   current_step="waiting for " + ", ".join(f"{w}'s findings" for w in waits))
            for member in ready:
                self._hand_over(member, depends.get(member.agent_id, []))
            with ThreadPoolExecutor(max_workers=len(ready), thread_name_prefix=f"task-{self.task_id[:8]}") as pool:
                futures = [pool.submit(self._run_member, member) for member in ready]
                for future in futures:
                    try:
                        future.result()
                    except (TaskPaused, TaskCancelled) as exc:
                        interrupted = interrupted or exc
            done |= {m.agent_id for m in ready}
            remaining = [m for m in remaining if m.agent_id not in done]
        if interrupted is not None:
            raise interrupted
        findings = self.rt.db.query(
            "SELECT agent_id, name, status, findings FROM task_agents WHERE task_id = %(t)s::uuid ORDER BY agent_id",
            {"t": self.task_id},
        )
        self.lead.team_findings = [
            f"{row['name']} ({row['status']}): {' '.join(str(row['findings'] or 'no findings').split())[:1500]}"
            for row in findings
        ]
        for member in self.members:
            for eid, line in member.evidence.items():
                self.lead.evidence.setdefault(eid, line)

    def _hand_over(self, member: AgentLoop, dependencies: Sequence[str]) -> None:
        """What an agent was waiting for: the findings of the agents it depends on, and the
        evidence behind them -- task-wide ids it may now cite itself."""
        if not dependencies:
            return
        rows = {str(r["agent_id"]): r for r in self.rt.db.query(
            "SELECT agent_id, name, status, findings FROM task_agents WHERE task_id = %(t)s::uuid "
            "AND agent_id = ANY(%(ids)s)", {"t": self.task_id, "ids": list(dependencies)},
        )}
        member.team_findings = [
            f"{rows[d]['name']} ({rows[d]['status']}): {' '.join(str(rows[d]['findings'] or 'no findings').split())[:1500]}"
            for d in dependencies if d in rows
        ]
        for other in self.members:
            if other.agent_id in dependencies:
                for eid, line in other.evidence.items():
                    member.evidence.setdefault(eid, line)

    def _spawn(self, specs: Sequence[Mapping[str, Any]], by_number: Mapping[int, str]) -> None:
        statements: list[tuple[str, Optional[Mapping[str, Any]]]] = [(
            "INSERT INTO task_agents (task_id, agent_id, name, role, goal, status, started_at) VALUES "
            "(%(t)s::uuid, 'lead', 'Lead agent', 'lead', %(g)s, 'running', now()) ON CONFLICT DO NOTHING",
            {"t": self.task_id, "g": self.goal[:600]},
        )]
        for spec in specs:
            statements.append((
                "INSERT INTO task_agents (task_id, agent_id, name, role, goal, focus, depends_on, status, plan) VALUES "
                "(%(t)s::uuid, %(a)s, %(n)s, 'helper', %(g)s, %(f)s, %(d)s, 'pending', %(p)s) ON CONFLICT DO NOTHING",
                {"t": self.task_id, "a": by_number[int(spec["number"])], "n": spec["name"], "g": spec["goal"],
                 "f": list(spec.get("focus") or []),
                 "d": [by_number[d] for d in spec.get("depends_on") or [] if d in by_number],
                 "p": _json({"steps": [spec["goal"]]})},
            ))
        self.rt.db.script(statements)
        summary = "; ".join(f"{s['name']}: {s['goal']}" for s in specs)
        self.note("plan", "Lead agent: " + "; ".join(self.plan.get("steps") or []) + f". Helpers -- {summary}",
                  author=f"{LEAD}-{self.task_id[:8]}", agent_id=LEAD)
        self.note("decision", f"Split the work across {len(specs)} helper agent(s); the lead writes the result from "
                  "their findings.", author=f"{LEAD}-{self.task_id[:8]}", agent_id=LEAD)
        self.lead.journal.append("agents", {"agents": [
            {"agent_id": by_number[int(s["number"])], "name": s["name"], "goal": s["goal"], "focus": s.get("focus") or [],
             "depends_on": [by_number[d] for d in s.get("depends_on") or [] if d in by_number]}
            for s in specs
        ]})

    def _run_member(self, member: AgentLoop) -> None:
        with task_context(self.task_id, member.ctx.agent_id):
            resuming = bool(member.history) or member.steps > 0
            self.set_agent(member.agent_id, status="running", current_step="resuming" if resuming else "starting",
                           **({} if resuming else {"started_at": _now()}))
            member.journal.append("agent", {"event": "resumed" if resuming else "started", "name": member.name,
                                            "goal": member.goal})
            try:
                findings = member.act()
                status, error = "done", None
            except (TaskPaused, TaskCancelled) as exc:
                self.set_agent(member.agent_id, status="paused" if isinstance(exc, TaskPaused) else "cancelled",
                               current_step=None)
                raise
            except BudgetExceeded as exc:
                status, error = "failed", str(exc)
                findings = self._partial(member)
            except (NoEligibleModel, ProviderError, StructuredOutputError) as exc:
                status, error = "failed", f"the model runtime failed: {str(exc)[:300]}"
                findings = self._partial(member)
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
                findings = self._partial(member)
            grounded = [c for c in cited(findings) if c in member.evidence]
            self.set_agent(member.agent_id, status=status, findings=findings[:6000], evidence=grounded,
                           current_step=None, finished_at=_now(), usage=_json({"steps": member.steps}))
            self.note("fact", findings[:1500] if status == "done" else f"(incomplete: {error}) {findings[:1400]}",
                      author=member.ctx.agent_id, evidence=grounded, agent_id=member.agent_id)
            member.journal.append("agent", {"event": status, "name": member.name, "findings": findings[:2000],
                                            "error": error})

    @staticmethod
    def _partial(member: AgentLoop) -> str:
        lines = [f"{item['tool']}: {item['summary']}" for item in member.history[-4:] if item.get("summary")]
        held = ", ".join(list(member.evidence)[:12])
        return ("Ran out before finishing. Last results: " + "; ".join(lines) + (f". Evidence gathered: {held}" if held else ""))[:1500]

    # -- endings ---------------------------------------------------------------------------------

    def _complete(self, answer: str) -> str:
        artifacts = self.rt.db.query(
            "SELECT a.id::text AS id, a.title, a.template_id, a.filename, a.status, a.requires_approval, a.version, "
            f"a.kind, {LATEST_VERSION} AS latest FROM artifacts a WHERE a.task_id = %(t)s::uuid ORDER BY a.created_at",
            {"t": self.task_id},
        )
        pending = [
            a for a in artifacts
            if a["requires_approval"] and a["status"] == "VERIFIED" and a["latest"]
            and not self.rt.db.scalar("SELECT count(*) FROM approvals WHERE artifact_id = %(a)s::uuid", {"a": a["id"]})
        ]
        status = "awaiting_approval" if pending else "completed"
        result = {
            "answer": answer,
            "citations": [c for c in cited(answer) if c in self.lead.evidence],
            "artifacts": artifacts,
            "awaiting_approval": [a["id"] for a in pending],
            "evidence": list(self.lead.evidence.values()),
            "agents": [m.agent_id for m in self.members],
        }
        if self.members:
            self.set_agent(LEAD, status="done", current_step=None, findings=answer[:6000], finished_at=_now())
        if self.kind == "task" and self.rt.remember:
            self._remember(status, answer, artifacts)
        return self._end(status, result=result)

    def _remember(self, status: str, answer: str, artifacts: Sequence[Mapping[str, Any]]) -> None:
        """The memory manager records what this task established. Best effort: a failure
        here is journalled and never changes how the task ended."""
        try:
            manager = MemoryManager(self.rt.db, self.rt.gateway, self.rt.audit)
            findings = [(m.name, str(self.rt.db.scalar(
                "SELECT findings FROM task_agents WHERE task_id = %(t)s::uuid AND agent_id = %(a)s",
                {"t": self.task_id, "a": m.agent_id}) or "")) for m in self.members]
            facts = [str(r["content"]) for r in self.rt.db.query(
                "SELECT content FROM task_shared_state WHERE task_id = %(t)s::uuid AND kind IN ('fact', 'decision') "
                "AND author LIKE 'human:%%' ORDER BY id", {"t": self.task_id})]
            outcomes = remember_task(
                manager, task={**self.task, "title": self.task.get("title") or self.goal[:90]}, status=status,
                answer=answer, findings=findings, facts=facts,
                deliverables=[f"'{a.get('title') or a.get('filename')}' (v{a.get('version')})" for a in artifacts
                              if a.get("template_id") and a.get("latest", True)],
                classification=self.classification, department=self.user.department, actor_id=self.user.user_id,
                on_model=lambda purpose, result: self.journal_model_call(self.lead.journal, purpose, result),
            )
            self.journal.append("memory", {
                "outcomes": [o.to_dict() for o in outcomes],
                "summary": ", ".join(f"{n} {op}" for op, n in Counter(o.operation for o in outcomes).items()),
            })
        except Exception as exc:
            self.journal.append("memory", {"error": f"{type(exc).__name__}: {str(exc)[:300]}", "outcomes": []})

    def _pause(self) -> str:
        set_status(self.rt.db, self.task_id, "paused")
        if self.members:
            self.rt.db.execute(
                "UPDATE task_agents SET status = 'paused' WHERE task_id = %(t)s::uuid AND status IN ('running', 'waiting')",
                {"t": self.task_id},
            )
        self.journal.append("paused", {"usage": {**self.usage.snapshot(), "steps": self.lead.steps}})
        return "paused"

    def _end(self, status: str, *, error: Optional[str] = None, result: Optional[dict[str, Any]] = None,
             budget: Optional[dict[str, Any]] = None) -> str:
        if self.lead.brief:
            self.working.put("revision_brief", None)
        elapsed = int(time.monotonic() - self.started)
        usage = {**self.usage.snapshot(), "steps": self.lead.steps, "elapsed_s": elapsed,
                 "agent_steps": {m.agent_id: m.steps for m in self.members}}
        fields: dict[str, Any] = {"usage": usage}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error"] = error
            if result is None:
                fields["result"] = {"answer": None, "evidence": list(self.lead.evidence.values()), "error": error}
        set_status(self.rt.db, self.task_id, status, **fields)
        if status in ("failed", "cancelled") and self.members:
            self.rt.db.execute(
                "UPDATE task_agents SET status = %(s)s, current_step = NULL WHERE task_id = %(t)s::uuid "
                "AND status IN ('pending', 'running', 'waiting', 'paused')",
                {"s": "cancelled" if status == "cancelled" else "failed", "t": self.task_id},
            )
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


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _json(value: Any) -> Any:
    from citadel_platform.db import Json

    return Json(value)


#: The name earlier code and tests use for one run of one task.
Agent = TaskRun


def run_task(rt: Runtime, task: Mapping[str, Any], worker_id: str) -> str:
    return TaskRun(rt, task, worker_id).run()


def refresh(rt: Runtime, task_id: str) -> Optional[dict[str, Any]]:
    return get_task(rt.db, task_id)


__all__ = [
    "Runtime",
    "Agent",
    "AgentLoop",
    "TaskRun",
    "BudgetLimits",
    "BudgetExceeded",
    "TaskCancelled",
    "TaskPaused",
    "LEAD",
    "run_task",
    "cited",
]
