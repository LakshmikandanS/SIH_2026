"""The five inputs to `decide()` (design doc §6.7).

    def decide(user, agent, task, action, resource) -> "ALLOW" | "DENY" | "REQUIRE_APPROVAL"

§6.7 fixes the signature but not the types. These are the smallest value
objects that carry exactly the attributes the rule chain reads -- nothing more,
so that adding a rule later is a visible change to the contract rather than a
quiet reinterpretation of an existing field.

They are frozen dataclasses, not ORM rows, for one reason that matters: the
Policy Engine must be a pure function of its inputs. A row could lazily load a
relationship mid-decision and make the same call decide differently depending
on session state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.db.models import Agent, Task, User


@dataclass(frozen=True)
class PolicyUser:
    """§3's User. Present because §6.7's signature names it; **no rule in this
    slice reads it**. That is faithful, not an oversight: BB-019 is resolved
    "minimally, not fully" and RBAC×ABAC combination logic is explicitly
    deferred. The field is carried so the decision *event* can record who was
    acting, and so a Phase-2 rule has somewhere to look."""

    user_id: str
    roles: tuple[str, ...] = ()
    clearance: str = ""
    department: str = ""

    @classmethod
    def from_row(cls, row: User) -> "PolicyUser":
        return cls(
            user_id=row.user_id,
            roles=tuple(row.roles or ()),
            clearance=row.clearance,
            department=row.department,
        )


@dataclass(frozen=True)
class PolicyAgent:
    """§3's Agent -- one per task (BB-014). Also unread by the four rules; also
    carried for the audit record."""

    agent_id: str
    task_id: str

    @classmethod
    def from_row(cls, row: Agent) -> "PolicyAgent":
        return cls(agent_id=row.agent_id, task_id=row.task_id)


@dataclass(frozen=True)
class PolicyTask:
    """§3's Task, plus `department`.

    IMPORTANT -- a gap in the frozen doc, resolved here and recorded in
    docs/BUILD_LOG.md: §6.7's fifth rule reads `task.department`, but §3's Task
    object has no `department` field. Department is an attribute of the *user*
    (§3 User) and it is what §6.5 puts in the capability's
    `scope.department`. So `PolicyTask.department` is the department of the
    task's owning user, which is exactly the value the capability was scoped
    with. No column was added to the Task table -- §3 is frozen; this is a
    value object assembled by the Tool Gateway at decision time.
    """

    task_id: str
    classification: str
    department: str

    @classmethod
    def from_rows(cls, task: Task, owner: User) -> "PolicyTask":
        return cls(
            task_id=task.task_id,
            classification=task.classification,
            department=owner.department,
        )


@dataclass(frozen=True)
class PolicyAction:
    """The agent's structured action (§5.1): one tool name plus arguments.

    Only `tool` is read by the rule chain. `arguments` is carried so a denial
    event records what was actually attempted.
    """

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyResource:
    """The concrete thing being acted on, known only at call time.

    This is the whole reason capability and policy are two checks (§6.6): the
    capability proves "this agent may attempt rag.search in general"; the
    resource is what lets policy decide "is THIS document allowed right now".

    `classification` and `acl` mirror §3's Evidence / §6.9's sidecar metadata.
    The descriptor is built by the trusted caller from the concrete target --
    never from agent-supplied arguments, which would let an agent describe its
    own target as harmless.
    """

    resource_id: str
    type: str
    classification: str
    acl: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        resource_id: str,
        type: str,
        classification: str,
        acl: Sequence[str],
    ) -> "PolicyResource":
        return cls(
            resource_id=resource_id,
            type=type,
            classification=classification,
            acl=tuple(acl),
        )

    @classmethod
    def for_task(cls, task: PolicyTask, *, type: str = "task_scope") -> "PolicyResource":
        """The resource for a tool that acts on nothing outside the task
        itself -- `python.execute` computing over evidence already retrieved,
        `generate_report` rendering it.

        Its classification is the task's and its ACL is the task's department,
        so the two data rules are evaluated against the task's own envelope
        rather than skipped. A tool with "no resource" must still be checked;
        passing `None` and short-circuiting the chain would be the exact hole
        §6.7's fail-closed default exists to prevent.
        """
        return cls(
            resource_id=task.task_id,
            type=type,
            classification=task.classification,
            acl=(task.department,),
        )
