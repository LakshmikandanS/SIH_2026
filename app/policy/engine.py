"""The Policy Engine (design doc §6.7).

    "Inputs and outputs exactly as specified; implementation is an ordered
     check list, not a rules engine. [...] Four rules, checked in this order,
     first match wins, default DENY. No policy language, no RBAC x ABAC
     combination logic beyond this -- that generality is explicitly deferred."

`_evaluate` below is §6.7's pseudocode, line for line, in the same order. The
order is not cosmetic:

  * `tool_disabled` is first so §1.3's emergency control beats every other
    consideration, including a tool that would otherwise be on the allow list.
  * `host.shell` is second so no scope, clearance or ACL arrangement can ever
    reach it.
  * `artifact.release` is third so a release request is routed to a human
    before any data rule can quietly ALLOW it.
  * The two data rules come before the allow list, so being an allowed tool is
    never sufficient on its own.
  * The tail is `return DENY`.

Purity is deliberate: this function emits no events, opens no session and
touches no I/O. The Tool Gateway calls it exactly once per tool call (§6.6
Step B) and is the one place a `POLICY_DECISION` event is written, so a
decision can never be recorded twice or recorded without having been made.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.state_machines import Classification
from app.policy.context import (
    PolicyAction,
    PolicyAgent,
    PolicyResource,
    PolicyTask,
    PolicyUser,
)
from app.policy.tool_disabled import get_registry
from app.policy.tools import ALLOWED_TOOLS, Tool


class Decision:
    """§6.7's return type: `"ALLOW" | "DENY" | "REQUIRE_APPROVAL"`."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class Rule:
    """Which line of the chain matched.

    §6.7 returns only the decision. The audit trail needs more than that:
    §1.3's demo asserts a denial reason of "tool disabled by administrator",
    and §6.6's envelope distinguishes `TOOL_DISABLED` from `POLICY_DENIED` --
    both of which are the *same* `DENY` from `decide()`. Naming the matching
    rule is how one ordered chain serves both without a second evaluation.
    """

    TOOL_DISABLED = "tool_disabled"
    HOST_SHELL_FORBIDDEN = "host_shell_forbidden"
    ARTIFACT_RELEASE_NEEDS_APPROVAL = "artifact_release_requires_approval"
    CLASSIFICATION_EXCEEDS_TASK = "resource_classification_exceeds_task"
    ACL_DISJOINT_FROM_DEPARTMENT = "resource_acl_disjoint_from_task_department"
    UNKNOWN_CLASSIFICATION = "unknown_classification"
    TOOL_ALLOWED = "tool_in_allow_list"
    DEFAULT_DENY = "default_deny"


@dataclass(frozen=True)
class PolicyOutcome:
    """`decide()`'s answer plus the two things an auditor needs to trust it."""

    decision: str
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


def _evaluate(
    user: PolicyUser,
    agent: PolicyAgent,
    task: PolicyTask,
    action: PolicyAction,
    resource: PolicyResource,
) -> PolicyOutcome:
    """§6.7's ordered check list. First match wins; the tail is DENY."""

    # The registry is fetched (rather than imported by value) so the Phase-2
    # seam in app/policy/tool_disabled.py actually takes effect.
    tool_disabled = get_registry()

    # if tool_disabled.get(action.tool): return "DENY"
    if tool_disabled.get(action.tool):
        return PolicyOutcome(
            Decision.DENY,
            Rule.TOOL_DISABLED,
            f"tool {action.tool!r} disabled by administrator",
        )

    # if action.tool == "host.shell": return "DENY"
    if action.tool == Tool.HOST_SHELL:
        return PolicyOutcome(
            Decision.DENY,
            Rule.HOST_SHELL_FORBIDDEN,
            f"{Tool.HOST_SHELL} is never permitted",
        )

    # if action.tool == "artifact.release": return "REQUIRE_APPROVAL"
    if action.tool == Tool.ARTIFACT_RELEASE:
        return PolicyOutcome(
            Decision.REQUIRE_APPROVAL,
            Rule.ARTIFACT_RELEASE_NEEDS_APPROVAL,
            f"{Tool.ARTIFACT_RELEASE} requires a human approval decision",
        )

    # if resource.classification > task.classification: return "DENY"
    #
    # `>` is the §3/§4 classification lattice (PUBLIC < INTERNAL <
    # CONFIDENTIAL), defined once in app.db.state_machines.Classification --
    # not Python string comparison, under which "PUBLIC" > "INTERNAL".
    try:
        exceeds = Classification.exceeds(resource.classification, task.classification)
    except ValueError as exc:
        # An unrecognised marking on either side is not comparable, so it
        # cannot be shown to be within the task's clearance. Fail closed.
        return PolicyOutcome(Decision.DENY, Rule.UNKNOWN_CLASSIFICATION, str(exc))
    if exceeds:
        return PolicyOutcome(
            Decision.DENY,
            Rule.CLASSIFICATION_EXCEEDS_TASK,
            f"resource classification {resource.classification} exceeds task "
            f"classification {task.classification}",
        )

    # if set(resource.acl).isdisjoint(set([task.department])): return "DENY"
    if set(resource.acl).isdisjoint({task.department}):
        return PolicyOutcome(
            Decision.DENY,
            Rule.ACL_DISJOINT_FROM_DEPARTMENT,
            f"resource acl {sorted(resource.acl)} is disjoint from task "
            f"department {task.department!r}",
        )

    # if action.tool in {"rag.search", "python.execute", "generate_report"}:
    #     return "ALLOW"
    if action.tool in ALLOWED_TOOLS:
        return PolicyOutcome(
            Decision.ALLOW,
            Rule.TOOL_ALLOWED,
            f"{action.tool} permitted within the task's classification and department",
        )

    # return "DENY"  -- fail closed: anything not explicitly matched is denied
    return PolicyOutcome(
        Decision.DENY,
        Rule.DEFAULT_DENY,
        f"no rule permits tool {action.tool!r}; the default is DENY",
    )


def decide(
    user: PolicyUser,
    agent: PolicyAgent,
    task: PolicyTask,
    action: PolicyAction,
    resource: PolicyResource,
) -> str:
    """§6.7's function, with §6.7's signature and §6.7's return type.

    `evaluate()` is the same chain returning the matched rule alongside the
    decision. There is exactly one chain; this is a projection of it, so the
    two can never disagree.
    """
    return _evaluate(user, agent, task, action, resource).decision


def evaluate(
    user: PolicyUser,
    agent: PolicyAgent,
    task: PolicyTask,
    action: PolicyAction,
    resource: PolicyResource,
) -> PolicyOutcome:
    """`decide()` plus the matched rule and a human-readable reason, for the
    `POLICY_DECISION` / `TOOL_DENIED` events and the §6.6 error envelope."""
    return _evaluate(user, agent, task, action, resource)
