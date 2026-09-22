"""citadel_tools.policy -- PLAN-M0 task 8's "Done" bar, literally: each of
`registry/policy.yaml`'s real rules has a test, an empty rule file denies
everything, and (per `registry/schema.py`'s own comment naming this module as
where the operators' runtime semantics live) every operator is proven both
ways -- matching and not matching -- not just exercised once in passing.

Two kinds of test: synthetic (a minimal rule + minimal facts, isolating one
operator or one piece of `evaluate()`'s own logic) and real (loaded from the
repo's actual `registry/policy.yaml` and `registry/roles.yaml`, one test per
rule id, in the rule's own position in the file -- proving the real registry
behaves exactly as `registry/policy.yaml`'s comments claim, not just that the
evaluator's logic is internally consistent).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from citadel_contracts.domain import Resource, User
from citadel_platform.registry import Registry, load_registry
from citadel_platform.registry.schema import PolicyRule, ToolEntry

from citadel_tools.policy import (
    DEFAULT_DENY_REASON,
    ActorFacts,
    Decision,
    PolicyEvaluationError,
    ReceiptFacts,
    actor_facts_from_user,
    evaluate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_DIR = REPO_ROOT / "registry"


# ---------------------------------------------------------------------------
# synthetic fixtures -- minimal, overridable
# ---------------------------------------------------------------------------


def _tool(**overrides: object) -> ToolEntry:
    defaults: dict[str, object] = dict(
        name="test.tool",
        package="citadel_tools.test",
        side_effect="read",
        required_capabilities=[],
        classification_ceiling="INTERNAL",
        requires_receipt=False,
        schema={"type": "object"},
    )
    defaults.update(overrides)
    return ToolEntry(**defaults)  # type: ignore[arg-type]


def _actor(**overrides: object) -> ActorFacts:
    defaults: dict[str, object] = dict(
        role="engineer", department="process-engineering", classification_max="CONFIDENTIAL", capabilities=()
    )
    defaults.update(overrides)
    return ActorFacts(**defaults)  # type: ignore[arg-type]


def _resource(**overrides: object) -> Resource:
    defaults: dict[str, object] = dict(
        resource_id="doc-1", type="document", classification="INTERNAL", acl=()
    )
    defaults.update(overrides)
    return Resource(**defaults)  # type: ignore[arg-type]


def _receipt(**overrides: object) -> ReceiptFacts:
    defaults: dict[str, object] = dict(valid=True)
    defaults.update(overrides)
    return ReceiptFacts(**defaults)  # type: ignore[arg-type]


def _rule(**overrides: object) -> PolicyRule:
    defaults: dict[str, object] = dict(id="r", effect="deny", when={}, reason="because")
    defaults.update(overrides)
    return PolicyRule(**defaults)  # type: ignore[arg-type]


def _decide(rules: list[PolicyRule], **facts: object) -> Decision:
    return evaluate(
        rules,
        actor=facts.get("actor", _actor()),  # type: ignore[arg-type]
        resource=facts.get("resource", _resource()),  # type: ignore[arg-type]
        tool=facts.get("tool", _tool()),  # type: ignore[arg-type]
        receipt=facts.get("receipt", _receipt()),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# the default -- not a rule in the file, proven here
# ---------------------------------------------------------------------------


def test_an_empty_rule_list_denies_everything():
    decision = _decide([])
    assert decision.effect == "deny"
    assert decision.rule_id is None
    assert decision.reason == DEFAULT_DENY_REASON
    assert decision.allowed is False


def test_no_rule_matching_denies_with_the_same_default():
    rules = [_rule(id="never-fires", effect="allow", when={"tool.side_effect": "write"})]
    decision = _decide(rules, tool=_tool(side_effect="read"))
    assert decision == Decision(effect="deny", rule_id=None, reason=DEFAULT_DENY_REASON)


# ---------------------------------------------------------------------------
# first match wins
# ---------------------------------------------------------------------------


def test_first_match_wins_even_when_a_later_rule_would_also_match():
    rules = [
        _rule(id="deny-first", effect="deny", when={"tool.side_effect": "read"}, reason="first"),
        _rule(id="allow-second", effect="allow", when={"tool.side_effect": "read"}),
    ]
    decision = _decide(rules, tool=_tool(side_effect="read"))
    assert decision.effect == "deny"
    assert decision.rule_id == "deny-first"


def test_rule_order_is_the_list_order_not_alphabetical_or_effect_order():
    rules = [
        _rule(id="z-allow", effect="allow", when={"tool.side_effect": "read"}),
        _rule(id="a-deny", effect="deny", when={"tool.side_effect": "read"}, reason="unreached"),
    ]
    decision = _decide(rules, tool=_tool(side_effect="read"))
    assert decision.rule_id == "z-allow"


# ---------------------------------------------------------------------------
# literal (non-operator) clauses -- plain equality
# ---------------------------------------------------------------------------


def test_a_plain_literal_clause_is_equality():
    rule = _rule(when={"tool.side_effect": "execute"})
    assert _decide([rule], tool=_tool(side_effect="execute")).rule_id == "r"
    assert _decide([rule], tool=_tool(side_effect="read")).rule_id is None  # falls through to default


def test_a_rule_with_multiple_clauses_requires_all_of_them():
    rule = _rule(when={"tool.side_effect": "read", "tool.requires_receipt": True})
    matching = _decide([rule], tool=_tool(side_effect="read", requires_receipt=True))
    assert matching.rule_id == "r"
    partial = _decide([rule], tool=_tool(side_effect="read", requires_receipt=False))
    assert partial.rule_id is None


# ---------------------------------------------------------------------------
# each operator, both directions
# ---------------------------------------------------------------------------


def test_not_in_lattice_matches_an_unrecognised_classification():
    rule = _rule(when={"resource.classification": {"not_in_lattice": True}})
    assert _decide([rule], resource=_resource(classification="TOP_SECRET")).rule_id == "r"


def test_not_in_lattice_does_not_match_a_recognised_classification():
    rule = _rule(when={"resource.classification": {"not_in_lattice": True}})
    assert _decide([rule], resource=_resource(classification="PUBLIC")).rule_id is None


def test_exceeds_matches_when_the_resource_outranks_the_actor():
    rule = _rule(when={"resource.classification": {"exceeds": "actor.classification_max"}})
    decision = _decide(
        [rule], resource=_resource(classification="CONFIDENTIAL"), actor=_actor(classification_max="INTERNAL")
    )
    assert decision.rule_id == "r"


def test_exceeds_does_not_match_when_the_actor_is_cleared_high_enough():
    rule = _rule(when={"resource.classification": {"exceeds": "actor.classification_max"}})
    decision = _decide(
        [rule], resource=_resource(classification="INTERNAL"), actor=_actor(classification_max="CONFIDENTIAL")
    )
    assert decision.rule_id is None


def test_exceeds_does_not_match_on_equal_levels():
    rule = _rule(when={"resource.classification": {"exceeds": "actor.classification_max"}})
    decision = _decide(
        [rule], resource=_resource(classification="INTERNAL"), actor=_actor(classification_max="INTERNAL")
    )
    assert decision.rule_id is None


def test_disjoint_from_matches_when_the_acl_shares_no_department():
    rule = _rule(when={"resource.acl": {"disjoint_from": "actor.department"}})
    decision = _decide([rule], resource=_resource(acl=("quality-assurance",)), actor=_actor(department="process-engineering"))
    assert decision.rule_id == "r"


def test_disjoint_from_does_not_match_when_the_department_is_in_the_acl():
    rule = _rule(when={"resource.acl": {"disjoint_from": "actor.department"}})
    decision = _decide(
        [rule],
        resource=_resource(acl=("quality-assurance", "process-engineering")),
        actor=_actor(department="process-engineering"),
    )
    assert decision.rule_id is None


def test_contains_all_matches_when_actor_capabilities_are_a_superset():
    rule = _rule(effect="allow", when={"actor.capabilities": {"contains_all": "tool.required_capabilities"}})
    decision = _decide(
        [rule], actor=_actor(capabilities=("retrieval", "workspace")), tool=_tool(required_capabilities=["retrieval"])
    )
    assert decision.rule_id == "r"


def test_contains_all_does_not_match_on_a_partial_overlap():
    rule = _rule(effect="allow", when={"actor.capabilities": {"contains_all": "tool.required_capabilities"}})
    decision = _decide(
        [rule], actor=_actor(capabilities=("retrieval",)), tool=_tool(required_capabilities=["retrieval", "vision"])
    )
    assert decision.rule_id is None


def test_contains_all_matches_trivially_when_the_tool_needs_no_capability():
    rule = _rule(effect="allow", when={"actor.capabilities": {"contains_all": "tool.required_capabilities"}})
    decision = _decide([rule], actor=_actor(capabilities=()), tool=_tool(required_capabilities=[]))
    assert decision.rule_id == "r"


def test_in_matches_a_listed_value():
    rule = _rule(effect="allow", when={"actor.role": {"in": ["engineer", "admin"]}})
    assert _decide([rule], actor=_actor(role="engineer")).rule_id == "r"


def test_in_does_not_match_an_unlisted_value():
    rule = _rule(effect="allow", when={"actor.role": {"in": ["engineer", "admin"]}})
    assert _decide([rule], actor=_actor(role="approver")).rule_id is None


# ---------------------------------------------------------------------------
# facts that don't resolve -- fail loud, never guessed at
# ---------------------------------------------------------------------------


def test_an_unknown_subject_raises_rather_than_silently_not_matching():
    rule = _rule(when={"nonexistent_subject.field": "x"})
    with pytest.raises(PolicyEvaluationError, match="unknown policy subject"):
        _decide([rule])


def test_an_unknown_field_on_a_known_subject_raises():
    rule = _rule(when={"tool.made_up_field": "x"})
    with pytest.raises(PolicyEvaluationError, match="no field 'made_up_field'"):
        _decide([rule])


# ---------------------------------------------------------------------------
# actor_facts_from_user -- the User -> ActorFacts boundary
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry("demo-local", REGISTRY_DIR)


def test_actor_facts_from_user_looks_up_capabilities_by_role(registry: Registry):
    user = User(
        user_id="U-1", username="r.kulkarni", roles=("engineer",), clearance="internal", department="process-engineering"
    )
    facts = actor_facts_from_user(user, registry)
    assert facts.role == "engineer"
    assert facts.department == "process-engineering"
    assert set(facts.capabilities) == set(registry.capabilities_for("engineer"))


def test_actor_facts_from_user_uppercases_a_lowercase_clearance(registry: Registry):
    """Migration 0006 seeds `clearance` lowercase (Postgres convention);
    `Classification` only defines the uppercase constants. Proven here rather
    than assumed, since a real login path reading a Postgres row has not been
    written yet."""
    user = User(user_id="U-2", username="s.nair", roles=("engineer",), clearance="confidential", department="instrumentation")
    facts = actor_facts_from_user(user, registry)
    assert facts.classification_max == "CONFIDENTIAL"


def test_actor_facts_from_user_rejects_zero_roles(registry: Registry):
    user = User(user_id="U-3", username="nobody", roles=(), clearance="internal", department="x")
    with pytest.raises(PolicyEvaluationError, match="0 role"):
        actor_facts_from_user(user, registry)


def test_actor_facts_from_user_rejects_more_than_one_role(registry: Registry):
    user = User(user_id="U-4", username="dual", roles=("engineer", "admin"), clearance="internal", department="x")
    with pytest.raises(PolicyEvaluationError, match="2 role"):
        actor_facts_from_user(user, registry)


def test_approver_gets_only_retrieval(registry: Registry):
    """ADR-0001 §Q7's demo cast: the approver reviews, and registry/roles.yaml's
    own header says why that must be capability-enforced, not just convention."""
    assert set(registry.capabilities_for("approver")) == {"retrieval"}


# ---------------------------------------------------------------------------
# the real registry.yaml -- one test per real rule, in file order
# ---------------------------------------------------------------------------


def _engineer(registry: Registry, **overrides: object) -> ActorFacts:
    user = User(
        user_id="U-ENG",
        username="engineer",
        roles=("engineer",),
        clearance=str(overrides.pop("clearance", "confidential")),
        department=str(overrides.pop("department", "process-engineering")),
    )
    return actor_facts_from_user(user, registry)


def test_real_rule_deny_unknown_classification(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry),
        resource=_resource(classification="TOP_SECRET"),
        tool=registry.tool("docs.search"),
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(
        effect="deny", rule_id="deny-unknown-classification", reason="unknown_classification_not_comparable"
    )


def test_real_rule_deny_above_clearance(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="internal"),
        resource=_resource(classification="CONFIDENTIAL", acl=("process-engineering",)),
        tool=registry.tool("docs.search"),
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="deny", rule_id="deny-above-clearance", reason="classification_exceeds_actor_max")


def test_real_rule_deny_acl_disjoint(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="confidential", department="process-engineering"),
        resource=_resource(classification="CONFIDENTIAL", acl=("quality-assurance",)),
        tool=registry.tool("docs.search"),
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="deny", rule_id="deny-acl-disjoint", reason="acl_disjoint_from_department")


def test_real_rule_deny_tool_ceiling(registry: Registry):
    # fs.read is capped at INTERNAL; a CONFIDENTIAL resource exceeds the tool's
    # own ceiling even though the actor is cleared for it and the acl matches.
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="confidential", department="process-engineering"),
        resource=_resource(classification="CONFIDENTIAL", acl=("process-engineering",)),
        tool=registry.tool("fs.read"),
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="deny", rule_id="deny-tool-ceiling", reason="tool_classification_ceiling_exceeded")


def test_real_rule_deny_missing_receipt(registry: Registry):
    # docs.search requires a receipt; an invalid one is denied even though
    # clearance, acl and tool ceiling are all otherwise fine.
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="confidential", department="process-engineering"),
        resource=_resource(classification="INTERNAL", acl=("process-engineering",)),
        tool=registry.tool("docs.search"),
        receipt=_receipt(valid=False),
    )
    assert decision == Decision(
        effect="deny", rule_id="deny-missing-receipt", reason="receipt_required_and_absent_or_invalid"
    )


def test_real_rule_allow_read_with_capability(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="internal", department="process-engineering"),
        resource=_resource(classification="INTERNAL", acl=("process-engineering",)),
        tool=registry.tool("fs.read"),  # side_effect=read, requires_receipt=false, needs [workspace]
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="allow", rule_id="allow-read-with-capability", reason=None)


def test_real_rule_allow_write_with_capability(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="internal", department="process-engineering"),
        resource=_resource(classification="INTERNAL", acl=("process-engineering",)),
        tool=registry.tool("fs.write"),  # side_effect=write, needs [workspace]
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="allow", rule_id="allow-write-with-capability", reason=None)


def test_real_rule_allow_execute_engineer(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=_engineer(registry, clearance="internal", department="process-engineering"),
        resource=_resource(classification="INTERNAL", acl=("process-engineering",)),
        tool=registry.tool("code.run"),  # side_effect=execute, requires_receipt=true, needs [sandbox]
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="allow", rule_id="allow-execute-engineer", reason=None)


def test_real_rule_allow_execute_denied_to_approver_by_role_even_with_capability(registry: Registry):
    """Two independent checks, deliberately not collapsed (packages/tools/AGENTS.md):
    even an approver granted `sandbox` by mistake in the registry would still be
    refused here by `actor.role`, not just by capability."""
    user = User(user_id="U-APR", username="approver", roles=("approver",), clearance="confidential", department="quality-assurance")
    actor = actor_facts_from_user(user, registry)
    decision = evaluate(
        registry.policy,
        actor=ActorFacts(
            role=actor.role, department=actor.department, classification_max=actor.classification_max, capabilities=("sandbox",)
        ),
        resource=_resource(classification="INTERNAL", acl=("quality-assurance",)),
        tool=registry.tool("code.run"),
        receipt=_receipt(valid=True),
    )
    assert decision.effect == "deny"
    assert decision.rule_id is None  # falls all the way through to the default


def test_real_rule_default_deny_when_actor_lacks_the_capability(registry: Registry):
    decision = evaluate(
        registry.policy,
        actor=ActorFacts(role="engineer", department="process-engineering", classification_max="INTERNAL", capabilities=()),
        resource=_resource(classification="INTERNAL", acl=("process-engineering",)),
        tool=registry.tool("docs.search"),  # needs [retrieval]; this actor has none
        receipt=_receipt(valid=True),
    )
    assert decision == Decision(effect="deny", rule_id=None, reason=DEFAULT_DENY_REASON)


def test_the_real_registrys_approver_can_read_docs_but_not_run_code(registry: Registry):
    """The end-to-end version of the ACL story web/AGENTS.md's ACL surface needs:
    the same capability set, two different outcomes, by tool."""
    user = User(user_id="U-APR2", username="approver", roles=("approver",), clearance="confidential", department="quality-assurance")
    actor = actor_facts_from_user(user, registry)
    resource = _resource(classification="CONFIDENTIAL", acl=("quality-assurance",))

    read_decision = evaluate(
        registry.policy, actor=actor, resource=resource, tool=registry.tool("docs.search"), receipt=_receipt(valid=True)
    )
    assert read_decision.allowed is True

    execute_decision = evaluate(
        registry.policy,
        actor=actor,
        resource=_resource(classification="INTERNAL", acl=("quality-assurance",)),
        tool=registry.tool("code.run"),
        receipt=_receipt(valid=True),
    )
    assert execute_decision.allowed is False
