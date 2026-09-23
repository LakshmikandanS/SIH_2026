"""citadel_platform.db -- literal rendering (pure) and real round trips (integration)."""

from __future__ import annotations

import pytest

from citadel_platform.db import (
    Database,
    Json,
    PsqlError,
    RealArray,
    TextArray,
    Vector,
    literal,
    render,
)
from pg_scratch import pg_scratch_db


def test_strings_double_embedded_quotes_and_drop_nul():
    assert literal("it's") == "'it''s'"
    assert literal("a\x00b") == "'ab'"


def test_scalars_and_null():
    assert literal(None) == "NULL"
    assert literal(True) == "TRUE"
    assert literal(False) == "FALSE"
    assert literal(42) == "42"
    assert literal(2.5) == "2.5"


def test_non_finite_floats_are_refused():
    with pytest.raises(ValueError):
        literal(float("nan"))


def test_arrays_json_vector_bytes():
    assert literal(["a", "b'c"]) == "ARRAY['a','b''c']::text[]"
    assert literal(TextArray([])) == "'{}'::text[]"
    assert literal(Json({"k": [1, None]})) == "'{\"k\": [1, null]}'::jsonb"
    assert literal(Vector([0.25, 1.0])) == "'[0.25,1.0]'::vector"
    assert literal(RealArray([0.1, 0.9])) == "ARRAY[0.1,0.9]::real[]"
    assert literal(b"\x01\xff") == "'\\x01ff'::bytea"


def test_unknown_types_are_refused_rather_than_stringified():
    with pytest.raises(TypeError):
        literal(object())


def test_render_substitutes_and_unescapes_percent():
    assert render("SELECT %(a)s, 100%%", {"a": "x"}) == "SELECT 'x', 100%"


def test_render_refuses_a_placeholder_with_no_parameter():
    with pytest.raises(KeyError):
        render("SELECT %(missing)s", {})


def test_injection_attempt_stays_a_literal():
    hostile = "x'); DROP TABLE users; --"
    assert render("SELECT %(v)s", {"v": hostile}) == "SELECT 'x''); DROP TABLE users; --'"


@pytest.mark.integration
def test_round_trip_keeps_null_distinct_from_empty_and_types_intact():
    with pg_scratch_db() as env:
        db = Database(env=env)
        row = db.query_one(
            "SELECT %(s)s AS s, NULL AS n, '' AS e, ARRAY['x','y'] AS arr, "
            "'{\"j\":1}'::jsonb AS j, 7 AS i, true AS t",
            {"s": "it's : fine %"},
        )
        assert row == {
            "s": "it's : fine %",
            "n": None,
            "e": "",
            "arr": ["x", "y"],
            "j": {"j": 1},
            "i": 7,
            "t": True,
        }


@pytest.mark.integration
def test_order_is_preserved_and_returning_works():
    with pg_scratch_db() as env:
        db = Database(env=env)
        assert [r["g"] for r in db.query("SELECT g FROM generate_series(1, 5) g ORDER BY g DESC")] == [5, 4, 3, 2, 1]
        db.execute("CREATE TABLE t (id serial PRIMARY KEY, v text)")
        inserted = db.query("INSERT INTO t (v) VALUES (%(a)s), (%(b)s) RETURNING id, v", {"a": "one", "b": "two"})
        assert [r["v"] for r in inserted] == ["one", "two"]


@pytest.mark.integration
def test_large_values_travel_over_stdin_not_argv():
    """A single argv entry over 128 KB is refused by Linux; this helper must not care."""
    with pg_scratch_db() as env:
        db = Database(env=env)
        assert len(db.scalar("SELECT %(s)s", {"s": "z" * 300_000})) == 300_000


@pytest.mark.integration
def test_script_is_all_or_nothing():
    with pg_scratch_db() as env:
        db = Database(env=env)
        db.execute("CREATE TABLE t (v text UNIQUE)")
        with pytest.raises(PsqlError):
            db.script(
                [
                    ("INSERT INTO t (v) VALUES (%(v)s)", {"v": "a"}),
                    ("INSERT INTO t (v) VALUES (%(v)s)", {"v": "a"}),  # violates UNIQUE
                ]
            )
        assert db.scalar("SELECT count(*) FROM t") == 0
