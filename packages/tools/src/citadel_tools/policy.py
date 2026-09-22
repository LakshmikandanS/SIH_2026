"""The policy evaluator: `registry/policy.yaml`'s rules, applied to a real decision.

PLAN-M0 task 8. Ordered, first match wins, **default deny** -- exactly
`registry/policy.yaml`'s own header comment and `packages/tools/AGENTS.md`'s "Policy is
data" section. The default is not written as a rule in the file ("a default that can be
deleted is not a default") -- it lives here, in `evaluate()` itself, and `test_policy.py`
proves an empty rule list denies everything.

This module is pure: no I/O, no audit-chain write, no database, no receipt
verification. It answers one question -- does this rule set allow this actor to do this
to this resource with this tool, given this receipt -- and returns a `Decision`
recording which rule (if any) decided it. Turning a `Decision` into an audit event, and
everything else in `packages/tools/AGENTS.md`'s resolve -> validate -> policy -> record
-> dispatch chain, is the chokepoint's job, not this module's -- the same split
`citadel_platform.audit.chain` (pure hashing) draws from `.postgres` (the real writer).
The chokepoint itself (`TOOL_REGISTRY`, `execute_tool`/`dispatch_tool`, JSON-schema
argument validation, tool resolution and dispatch) is separate, later work.

## The four subjects `registry/policy.yaml`'s `when` clauses name

`resource.*` and `tool.*` map directly onto existing typed objects --
`citadel_contracts.domain.Resource` and `citadel_platform.registry.schema.ToolEntry`
already carry exactly the fields the rules reference (`classification`, `acl`;
`side_effect`, `classification_ceiling`, `requires_receipt`, `required_capabilities`) --
so no wrapper type exists for either; `evaluate()` takes them as-is. `receipt.*` is one
field (`ReceiptFacts.valid`), computed by the caller from an actual `verify_receipt` call
-- this module never verifies a receipt itself, only reads the one bit the rules need.

`actor.*` is the one subject with no existing match: `citadel_contracts.domain.User`
carries `roles` (plural) and `clearance`, but the rules read `actor.role` (singular --
migration 0005's own header comment explains why the Postgres column is singular) and
`actor.classification_max` (a name distinct from `clearance` because it is what the rule
*means*, not a copy of the column name). `actor.capabilities` does not exist on `User` at
all -- it is a per-role grant, `registry/roles.yaml`, the same "policy is data" reasoning
applied to one more mapping (`registry/tools.yaml`'s `required_capabilities` vocabulary
had no source of truth for which roles grant which capability until that file existed).
`actor_facts_from_user()` is the one place a `User` becomes the `ActorFacts` these rules
can actually evaluate against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_contracts.domain import Resource, User
from citadel_platform.registry import Registry
from citadel_platform.registry.schema import PolicyRule, ToolEntry


class PolicyEvaluationError(Exception):
    """A rule's `when` clause names a subject or field the caller's facts do not carry.

    Raised rather than guessed at: a policy decision made on a silently-missing fact is
    the exact failure mode this module exists to prevent, so an incomplete set of facts
    is a bug to fix at the call site, never a rule to silently skip.
    """


@dataclass(frozen=True)
class ActorFacts:
    """Exactly the fields `registry/policy.yaml`'s `actor.*` rules read. Built by the
    trusted caller (`actor_facts_from_user`, ordinarily) -- never accepted as
    agent-supplied input, the same posture `citadel_contracts.domain.Resource`
    documents for itself."""

    role: str
    department: str
    classification_max: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceiptFacts:
    """`registry/policy.yaml`'s only `receipt.*` field. `valid` is the result of an
    actual `citadel_contracts.receipts.verify_receipt` call at the chokepoint -- this
    module never verifies a receipt itself, only reads the one bit the rules need."""

    valid: bool


@dataclass(frozen=True)
class Decision:
    """What `evaluate()` returns. `rule_id` is `None` only for the implicit
    default-deny; every rule-matched decision names the rule that decided it, which is
    what an eventual audit event records `reason` and `rule_id` from."""

    effect: Literal["allow", "deny"]
    rule_id: Optional[str]
    reason: Optional[str]

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


#: Not a rule in `registry/policy.yaml` on purpose -- see the module docstring.
DEFAULT_DENY_REASON = "no_policy_rule_matched"


def actor_facts_from_user(user: User, registry: Registry) -> ActorFacts:
    """The one place a `User` becomes the facts `evaluate()` can use.

    Two normalisations happen here, not inside `evaluate()`, because both are specific
    to *this* boundary (a `User`, not a `Resource`, becoming a fact):

    - `role`: `User.roles` is a tuple (`packages/contracts/AGENTS.md`'s porting note:
      no `UNIQUE`-style scope cut baked into the type), but every seeded identity
      (migration 0006) and `registry/policy.yaml`'s own `actor.role` rules assume
      exactly one. A user with zero or more than one role is refused here, loudly,
      rather than silently taking `roles[0]` and hiding a real data problem.
    - `classification_max`: `User.clearance` is read from Postgres as migration 0006
      wrote it -- lowercase (`'internal'`, `'confidential'`), Postgres convention, not
      the lattice's own uppercase constants (`citadel_contracts.classification.
      Classification`). Uppercased once, here, at the boundary that turns a `User` into
      a policy fact -- exactly the pattern `registry/schema.py`'s `_known_classification`
      already established for registry data, applied to the analogous user-data
      boundary (there is no ingest-time equivalent for a `User` until a real login path
      reads one from Postgres).
    """
    if len(user.roles) != 1:
        raise PolicyEvaluationError(
            f"user {user.user_id!r} has {len(user.roles)} role(s) ({user.roles!r}); "
            f"the policy evaluator needs exactly one"
        )
    (role,) = user.roles
    return ActorFacts(
        role=role,
        department=user.department,
        classification_max=user.clearance.upper(),
        capabilities=registry.capabilities_for(role),
    )


def _resolve(path: str, facts: Mapping[str, Any]) -> Any:
    subject, sep, field = path.partition(".")
    if not sep:
        raise PolicyEvaluationError(f"not a <subject>.<field> path: {path!r}")
    if subject not in facts:
        raise PolicyEvaluationError(
            f"unknown policy subject {subject!r} (from path {path!r}); "
            f"known subjects are {sorted(facts)}"
        )
    try:
        return getattr(facts[subject], field)
    except AttributeError:
        raise PolicyEvaluationError(f"{subject!r} has no field {field!r} (from path {path!r})") from None


def _as_set(value: Any) -> set[Any]:
    """`registry/policy.yaml`'s own comment on `deny-acl-disjoint`: a scalar operand
    (`actor.department`) is treated as a singleton set against a sequence operand
    (`resource.acl`). Generalised here so any scalar-vs-sequence or sequence-vs-sequence
    combination compares the same way, not just today's one real usage."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return set(value)
    return {value}


def _op_not_in_lattice(left: Any, operand: Any, facts: Mapping[str, Any]) -> bool:
    try:
        Classification.rank(str(left))
        recognised = True
    except ValueError:
        recognised = False
    return (not recognised) == bool(operand)


def _op_exceeds(left: Any, operand: Any, facts: Mapping[str, Any]) -> bool:
    right = _resolve(str(operand), facts)
    return Classification.exceeds(str(left), str(right))


def _op_disjoint_from(left: Any, operand: Any, facts: Mapping[str, Any]) -> bool:
    right = _resolve(str(operand), facts)
    return _as_set(left).isdisjoint(_as_set(right))


def _op_contains_all(left: Any, operand: Any, facts: Mapping[str, Any]) -> bool:
    right = _resolve(str(operand), facts)
    return _as_set(right) <= _as_set(left)


def _op_in(left: Any, operand: Any, facts: Mapping[str, Any]) -> bool:
    return left in operand


#: Closed set, matching `registry/schema.py`'s `_KNOWN_POLICY_OPERATORS` exactly -- that
#: module validates a rule is *well-formed enough to be evaluable*; this is where the
#: operators actually run, as that module's own comment says it must be.
_OPERATORS = {
    "not_in_lattice": _op_not_in_lattice,
    "exceeds": _op_exceeds,
    "disjoint_from": _op_disjoint_from,
    "contains_all": _op_contains_all,
    "in": _op_in,
}


def _clause_matches(path: str, expected: Any, facts: Mapping[str, Any]) -> bool:
    left = _resolve(path, facts)
    if isinstance(expected, dict):
        ((operator, operand),) = expected.items()  # the loader guarantees exactly one, known key
        return _OPERATORS[operator](left, operand, facts)
    return bool(left == expected)


def _rule_matches(rule: PolicyRule, facts: Mapping[str, Any]) -> bool:
    return all(_clause_matches(path, expected, facts) for path, expected in rule.when.items())


def evaluate(
    rules: Sequence[PolicyRule],
    *,
    actor: ActorFacts,
    resource: Resource,
    tool: ToolEntry,
    receipt: ReceiptFacts,
) -> Decision:
    """First match wins; no match is a deny. `rules` is ordinarily `registry.policy` --
    passed explicitly, not read from a global, so a test can hand this an empty tuple, a
    single synthetic rule, or the real registry's rule set with equal ease."""
    facts: dict[str, Any] = {"actor": actor, "resource": resource, "tool": tool, "receipt": receipt}
    for rule in rules:
        if _rule_matches(rule, facts):
            return Decision(effect=rule.effect, rule_id=rule.id, reason=rule.reason)
    return Decision(effect="deny", rule_id=None, reason=DEFAULT_DENY_REASON)


__all__ = [
    "ActorFacts",
    "ReceiptFacts",
    "Decision",
    "DEFAULT_DENY_REASON",
    "PolicyEvaluationError",
    "actor_facts_from_user",
    "evaluate",
]
