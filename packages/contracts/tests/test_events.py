"""citadel_contracts/events.py -- the vocabulary is open (a registry a
deployment populates), never a closed enum, and it still fails closed on
anything nobody registered.

Ported from the prototype's own test file *in discipline only*. The
sixteen-closed-types tests do not port: the closed vocabulary is exactly
what packages/contracts/AGENTS.md's porting table calls out to change --
"the discipline, not the file... A registry with registration, not a
closed StrEnum." What DOES port is the underlying concern each old test
protected, translated to the open shape: fail-closed on the unknown,
provably not vacuous, no silent scope creep.
"""

from __future__ import annotations

import threading

import pytest

from citadel_contracts.events import EventRegistry, UnknownEventType


def test_a_freshly_constructed_registry_knows_nothing():
    registry = EventRegistry()
    assert registry.all_types == frozenset()
    assert len(registry) == 0
    assert not registry.is_registered("TASK_CREATED")


def test_registering_a_type_makes_it_known():
    registry = EventRegistry()
    registry.register("TASK_CREATED")
    assert registry.is_registered("TASK_CREATED")
    assert "TASK_CREATED" in registry
    assert registry.all_types == frozenset({"TASK_CREATED"})


def test_register_many_accepts_any_iterable():
    registry = EventRegistry()
    registry.register_many(["TASK_CREATED", "PLAN_CREATED", "AGENT_STARTED"])
    assert len(registry) == 3


def test_registering_the_same_type_twice_is_not_an_error():
    """Two packages naming the same event at startup is normal, not a
    collision -- registration is idempotent, unlike a database UNIQUE
    constraint that would raise on the second call."""
    registry = EventRegistry()
    registry.register("TASK_CREATED")
    registry.register("TASK_CREATED")
    assert len(registry) == 1


def test_an_unregistered_event_type_fails_closed():
    """The vocabulary is open, not unchecked: a name nobody registered is
    still rejected -- this is what keeps an unauditable row out of the
    audit chain, the same reasoning the prototype's closed enum used,
    applied to an open table instead."""
    registry = EventRegistry()
    with pytest.raises(UnknownEventType):
        registry.assert_registered("SOMETHING_NOBODY_REGISTERED")


def test_assert_registered_returns_the_type_on_success():
    registry = EventRegistry()
    registry.register("TASK_CREATED")
    assert registry.assert_registered("TASK_CREATED") == "TASK_CREATED"


def test_registering_a_new_type_after_startup_is_the_point_not_scope_creep():
    """The prototype's own suite had a test forbidding a RECEIPT_* constant
    from appearing before a phase that needed one -- with a closed enum,
    any new name is a code change and therefore scope creep by definition.
    Here the concern moves from "the file must not define it" to "nothing
    may emit it before someone registers it": registering a brand-new name
    is exactly what an open vocabulary is for."""
    registry = EventRegistry()
    registry.register("TASK_CREATED")
    registry.register("RECEIPT_VERIFIED")
    assert registry.is_registered("RECEIPT_VERIFIED")


@pytest.mark.parametrize("bad_value", ["", None])
def test_rejects_a_non_string_or_empty_registration(bad_value):
    registry = EventRegistry()
    with pytest.raises(ValueError):
        registry.register(bad_value)  # type: ignore[arg-type]


def test_registries_are_independent_instances_not_a_shared_global():
    """Mirrors receipts.py's own
    test_nonce_stores_are_independent_per_instance: an open vocabulary that
    leaked state between two registries would make one deployment's (or
    one test's) registrations silently visible to another."""
    a = EventRegistry()
    b = EventRegistry()
    a.register("TASK_CREATED")
    assert a.is_registered("TASK_CREATED")
    assert not b.is_registered("TASK_CREATED")


def test_all_types_is_a_snapshot_not_a_live_view():
    registry = EventRegistry()
    registry.register("TASK_CREATED")
    snapshot = registry.all_types
    registry.register("PLAN_CREATED")
    assert "PLAN_CREATED" not in snapshot


def test_concurrent_registration_from_multiple_threads_loses_nothing():
    """Production builds one EventRegistry at startup from
    registry/events.yaml, but nothing stops two components from registering
    concurrently during that window. The lock (mirrors InMemoryNonceStore's)
    must make that safe -- relevant here specifically because the demo's
    own multi-engineer scenario (ADR-0001 Q7) is a concurrency story."""
    registry = EventRegistry()
    names = [f"EVENT_{i}" for i in range(200)]

    def register_half(subset):
        for name in subset:
            registry.register(name)

    t1 = threading.Thread(target=register_half, args=(names[:100],))
    t2 = threading.Thread(target=register_half, args=(names[100:],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(registry) == 200


def test_unknown_event_type_error_names_the_offending_value():
    with pytest.raises(UnknownEventType) as excinfo:
        raise UnknownEventType("SOMETHING_MADE_UP")
    assert excinfo.value.event_type == "SOMETHING_MADE_UP"
