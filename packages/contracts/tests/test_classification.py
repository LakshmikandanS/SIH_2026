"""citadel_contracts/classification.py -- the lattice is fail-closed and ordered."""

from __future__ import annotations

import pytest

from citadel_contracts.classification import Classification


def test_the_lattice_is_ordered_public_below_internal_below_confidential():
    assert Classification.rank(Classification.PUBLIC) < Classification.rank(
        Classification.INTERNAL
    )
    assert Classification.rank(Classification.INTERNAL) < Classification.rank(
        Classification.CONFIDENTIAL
    )


@pytest.mark.parametrize(
    "resource_level,task_level,expected",
    [
        (Classification.CONFIDENTIAL, Classification.PUBLIC, True),
        (Classification.INTERNAL, Classification.PUBLIC, True),
        (Classification.PUBLIC, Classification.CONFIDENTIAL, False),
        (Classification.CONFIDENTIAL, Classification.CONFIDENTIAL, False),
        (Classification.PUBLIC, Classification.PUBLIC, False),
    ],
)
def test_exceeds_matches_the_ordered_comparison(resource_level, task_level, expected):
    assert Classification.exceeds(resource_level, task_level) is expected


def test_exceeds_is_not_python_string_comparison():
    """"PUBLIC" > "INTERNAL" as strings (P > I), but PUBLIC does not exceed
    INTERNAL in the lattice. If this ever used `>` on the raw strings instead
    of `Classification.rank`, this test would catch it."""
    assert Classification.exceeds(Classification.PUBLIC, Classification.INTERNAL) is False


def test_an_unknown_marking_fails_closed_on_rank():
    with pytest.raises(ValueError):
        Classification.rank("TOP_SECRET")


def test_an_unknown_marking_fails_closed_on_exceeds_from_either_side():
    with pytest.raises(ValueError):
        Classification.exceeds("TOP_SECRET", Classification.PUBLIC)
    with pytest.raises(ValueError):
        Classification.exceeds(Classification.PUBLIC, "TOP_SECRET")


def test_registry_ceiling_values_are_valid_lattice_members():
    """registry/profiles.yaml stores classification_ceiling in lowercase
    (demo-local: confidential, hpc-eval: public) by that file's own
    convention; whoever reads it must uppercase before calling rank(). This
    test pins the contract each ceiling value must satisfy once uppercased,
    so a future typo like the "restricted" value this repo caught once
    fails here, at the cheapest possible layer, instead of at runtime."""
    for ceiling in ("confidential", "public"):
        Classification.rank(ceiling.upper())  # must not raise
