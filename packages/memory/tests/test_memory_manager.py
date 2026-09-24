"""The memory manager against a real Postgres with pgvector.

What these pin down (docs/adr/0009):

* visibility is decided in SQL, in the same statement as the vector search -- a memory
  the caller may not see never reaches Python at all, which the spy below checks on the
  raw rows, with a negative control proving the same query does return it to someone
  cleared for it;
* a candidate is compared with, and so can only ever be merged into, memories of its
  own compartment (classification and ACL);
* each of Monarch's six operations is executed deterministically, and an unusable model
  decision falls back to `create`, the lossless choice;
* people curate: state, edit, archive and restore -- within what they may see.

The model is a plain Python stand-in (no HTTP): its decisions are scripted per test;
the embeddings are the same deterministic feature hashes the fake Ollama serves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import pytest

from citadel_gateway import StructuredOutputError
from citadel_memory import Candidate, Compartment, MemoryManager, MemoryScope
from citadel_platform.db import Database
from fake_ollama import hash_embedding
from pg_scratch import apply_all_migrations, pg_scratch_db, requires_pgvector

pytestmark = [requires_pgvector, pytest.mark.integration]

PE_INTERNAL = Compartment("INTERNAL", ("process-engineering",))
PE_CONFIDENTIAL = Compartment("CONFIDENTIAL", ("process-engineering",))
INSTRUMENTS = Compartment("INTERNAL", ("instrumentation",))
SHARED_PUBLIC = Compartment("PUBLIC", ("instrumentation", "process-engineering"))


@dataclass
class ScriptedModel:
    """Stands in for the gateway: deterministic embeddings, scripted decisions."""

    decide: Callable[[Mapping[str, Any]], Mapping[str, Any]] = lambda payload: {
        "operation": "create", "target": "none", "confidence": 1.0}
    extract: Mapping[str, Any] = field(default_factory=lambda: {"decision": "discard", "memories": []})
    fail: bool = False
    calls: list[str] = field(default_factory=list)
    shown: list[Mapping[str, Any]] = field(default_factory=list)

    def embed(self, texts: Sequence[str], *, classification: str, task_id: Optional[str] = None,
              actor_id: Optional[str] = None) -> Any:
        return SimpleNamespace(vectors=[hash_embedding(t) for t in texts], model_id="embed-test")

    def generate(self, request: Any, messages: Sequence[Any], **kwargs: Any) -> Any:
        self.calls.append(request.purpose)
        if self.fail:
            raise StructuredOutputError("the model did not produce valid JSON")
        if request.purpose == "memory.mutate":
            payload = json.loads(messages[-1].content)
            self.shown.append(payload)
            data: Any = dict(self.decide(payload))
        else:
            data = dict(self.extract)
        return SimpleNamespace(data=data, model_id="reason-test")


class Spy(Database):
    """Records every row the database returns to Python."""

    rows: list[dict[str, Any]]

    def query(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
        result = super().query(sql, params)
        if "FROM memories" in sql:
            self.rows.extend(result)
        return result


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, str]]:
    with pg_scratch_db() as scratch:
        apply_all_migrations(scratch)
        yield scratch


@pytest.fixture
def db(env: dict[str, str]) -> Database:
    database = Database(env=dict(env))
    database.execute("DELETE FROM memory_events")
    database.execute("DELETE FROM memories")
    return database


def _manager(db: Database, model: Optional[ScriptedModel] = None) -> MemoryManager:
    return MemoryManager(db, model or ScriptedModel())


def _store(manager: MemoryManager, content: str, compartment: Compartment, *, subject: str = "Lathe L-1",
           memory_type: str = "equipment_fact") -> str:
    outcome = manager.propose(Candidate(content=content, subject=subject, memory_type=memory_type),
                              compartment=compartment, actor_id="seed", use_model=False)
    assert outcome.operation == "create" and outcome.memory_id
    return outcome.memory_id


def _row(db: Database, memory_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT content, status, superseded_by::text AS superseded_by FROM memories "
                       "WHERE id = %(id)s::uuid", {"id": memory_id})
    assert row is not None
    return row


def test_recall_filters_in_sql_and_never_reads_what_the_caller_may_not_see(env: dict[str, str], db: Database):
    manager = _manager(db)
    _store(manager, "Lathe L-1 repair cost is 31.2% of replacement cost.", PE_INTERNAL)
    _store(manager, "Lathe L-1 negotiated price from Konkan Tooling is 16.9 lakh.", PE_CONFIDENTIAL)
    _store(manager, "Lathe L-1 feeds sleeves to the instrument shop.", INSTRUMENTS)
    _store(manager, "Lathe L-1 is a Sahyadri SMT-450 commissioned in 2009.", SHARED_PUBLIC)

    spy = Spy(env=dict(env))
    spy.rows = []
    watched = _manager(spy)
    internal = watched.recall("lathe L-1", MemoryScope("process-engineering", "INTERNAL", "engineer-1"))
    assert {m.classification for m in internal} == {"INTERNAL", "PUBLIC"}
    assert all("process-engineering" in m.acl for m in internal)
    # The predicate is in the statement: the raw rows Postgres returned hold nothing the
    # caller may not see -- no post-filtering in Python ever had anything to remove.
    assert spy.rows and all(r["classification"] in ("internal", "public") for r in spy.rows)
    assert all("process-engineering" in r["acl"] for r in spy.rows)

    # Negative control: the same query, for someone cleared, does return the rest.
    cleared = watched.recall("lathe L-1", MemoryScope("process-engineering", "CONFIDENTIAL", "engineer-2"))
    assert "CONFIDENTIAL" in {m.classification for m in cleared}
    other = watched.recall("lathe L-1", MemoryScope("instrumentation", "INTERNAL", "tech-1"))
    assert {tuple(m.acl) for m in other} <= {("instrumentation",), ("instrumentation", "process-engineering")}


def test_recall_scores_meaning_and_recency_and_touches_what_it_used(db: Database):
    manager = _manager(db)
    old = _store(manager, "Lathe L-1 spindle runout is 0.045 mm.", PE_INTERNAL)
    new = _store(manager, "Lathe L-1 spindle runout is 0.045 mm at the chuck.", PE_INTERNAL)
    db.execute("UPDATE memories SET last_accessed = now() - interval '90 days' WHERE id = %(id)s::uuid", {"id": old})
    ranked = manager.recall("lathe L-1 spindle runout", MemoryScope("process-engineering", "INTERNAL", "e1"))
    assert [m.id for m in ranked][:2] == [new, old]
    assert ranked[0].recency is not None and ranked[1].recency is not None and ranked[0].recency > ranked[1].recency
    assert [m.ref for m in ranked][:2] == ["M1", "M2"]
    touched = db.scalar("SELECT access_count FROM memories WHERE id = %(id)s::uuid", {"id": old})
    assert touched == 1


def test_a_candidate_is_only_ever_compared_with_its_own_compartment(db: Database):
    model = ScriptedModel(decide=lambda payload: {"operation": "merge", "target": "M1", "replacement": "merged",
                                                  "confidence": 0.9})
    manager = _manager(db, model)
    secret = _store(manager, "Lathe L-1 negotiated price is 16.9 lakh.", PE_CONFIDENTIAL)
    outcome = manager.propose(Candidate(content="Lathe L-1 negotiated price is 16.9 lakh.", subject="Lathe L-1"),
                              compartment=PE_INTERNAL, actor_id="engineer-1")
    # Nothing in the INTERNAL compartment is related, so the model is never asked -- and
    # the CONFIDENTIAL memory was never shown to it, let alone merged into.
    assert outcome.operation == "create" and "memory.mutate" not in model.calls
    assert _row(db, secret)["content"] == "Lathe L-1 negotiated price is 16.9 lakh."


@pytest.mark.parametrize("operation", ["update", "merge", "contradict", "ignore", "archive"])
def test_each_mutation_is_executed_deterministically(db: Database, operation: str):
    decision = {"operation": operation, "target": "M1", "confidence": 0.8, "reason": "scripted"}
    if operation in ("update", "merge"):
        decision["replacement"] = "Lathe L-1 repair cost is 31.2% of replacement cost (Aug 2026)."
    if operation == "ignore":
        decision["target"] = "none"
    model = ScriptedModel(decide=lambda payload: decision)
    manager = _manager(db, model)
    existing = _store(manager, "Lathe L-1 repair cost is about a third of its replacement cost.", PE_INTERNAL)
    outcome = manager.propose(Candidate(content="Lathe L-1 repair cost is 31.2% of replacement cost.",
                                        subject="Lathe L-1"), compartment=PE_INTERNAL, actor_id="engineer-1")
    assert outcome.operation == operation and not outcome.fallback
    assert model.shown and model.shown[0]["existing_memories"][0]["ref"] == "M1"
    row = _row(db, existing)
    count = db.scalar("SELECT count(*) FROM memories")
    if operation in ("update", "merge"):
        assert row["content"] == decision["replacement"] and count == 1 and outcome.memory_id == existing
    elif operation == "contradict":
        assert row["status"] == "superseded" and row["superseded_by"] == outcome.memory_id and count == 2
    elif operation == "ignore":
        assert row["status"] == "active" and count == 1 and outcome.memory_id is None
    elif operation == "archive":
        assert row["status"] == "archived" and count == 1
    event = db.query_one("SELECT operation, reason FROM memory_events ORDER BY id DESC LIMIT 1")
    assert event is not None and event["operation"] == operation and event["reason"] == "scripted"


@pytest.mark.parametrize("decision", [
    {"operation": "update", "target": "none", "confidence": 0.9},             # update of nothing
    {"operation": "merge", "target": "M1", "confidence": 0.9},                # merge without the merged wording
    {"operation": "rewrite_everything", "target": "M1", "confidence": 0.9},   # not an operation
])
def test_an_unusable_decision_falls_back_to_create(db: Database, decision: dict[str, Any]):
    manager = _manager(db, ScriptedModel(decide=lambda payload: decision))
    existing = _store(manager, "Lathe L-1 bed wear is 0.12 mm.", PE_INTERNAL)
    outcome = manager.propose(Candidate(content="Lathe L-1 bed wear is 0.12 mm near the headstock.",
                                        subject="Lathe L-1"), compartment=PE_INTERNAL, actor_id="engineer-1")
    assert outcome.operation == "create" and outcome.fallback and outcome.memory_id != existing
    assert _row(db, existing)["status"] == "active" and db.scalar("SELECT count(*) FROM memories") == 2


def test_a_model_that_fails_loses_nothing(db: Database):
    model = ScriptedModel(fail=True)
    manager = _manager(db, model)
    _store(manager, "Lathe L-1 utilisation is 78%.", PE_INTERNAL)
    outcome = manager.propose(Candidate(content="Lathe L-1 utilisation is 78% over two shifts.", subject="Lathe L-1"),
                              compartment=PE_INTERNAL, actor_id="engineer-1")
    assert outcome.operation == "create" and outcome.fallback
    assert manager.extract("anything", classification="INTERNAL", actor_id="engineer-1") == []


def test_extraction_is_validated_not_trusted(db: Database):
    model = ScriptedModel(extract={"decision": "store", "memories": [
        {"content": "Lathe L-1 needs three quotations.", "memory_type": "Constraint Fact", "tier": "semantic",
         "evidence": ["E2", "not-an-id"]},
        {"content": "short", "memory_type": "lesson", "tier": "semantic"},
        {"content": "An outcome worth keeping for later.", "memory_type": "outcome", "tier": "archival"},
    ]})
    candidates = _manager(db, model).extract("text", classification="INTERNAL", actor_id="engineer-1")
    assert [c.content for c in candidates] == ["Lathe L-1 needs three quotations.", "An outcome worth keeping for later."]
    assert candidates[0].memory_type == "constraint_fact" and candidates[0].evidence == ["E2"]
    assert candidates[1].tier == "semantic"  # an unknown tier is not invented


def test_people_curate_what_is_remembered_within_what_they_may_see(db: Database):
    model = ScriptedModel(decide=lambda payload: {"operation": "ignore", "target": "none", "confidence": 0.9})
    manager = _manager(db, model)
    stated = manager.remember("Lathe L-1 sleeves need 0.02 mm.", compartment=PE_INTERNAL, actor_id="engineer-1",
                              subject="Lathe L-1")
    assert stated.operation == "create" and stated.memory_id
    again = manager.remember("Lathe L-1 sleeves need 0.02 mm.", compartment=PE_INTERNAL, actor_id="engineer-1",
                             subject="Lathe L-1")
    assert again.operation == "ignore"  # saying it twice is not two memories

    mine = MemoryScope("process-engineering", "INTERNAL", "engineer-1")
    outsider = MemoryScope("instrumentation", "CONFIDENTIAL", "tech-1")
    assert manager.edit(stated.memory_id, "Lathe L-1 pump sleeves need 0.02 mm.", scope=mine, actor_id="engineer-1")
    assert not manager.edit(stated.memory_id, "rewritten by someone else", scope=outsider, actor_id="tech-1")
    assert _row(db, stated.memory_id)["content"] == "Lathe L-1 pump sleeves need 0.02 mm."
    assert manager.set_status(stated.memory_id, "archived", scope=mine, actor_id="engineer-1")
    assert not manager.recall("pump sleeves", mine)
    assert manager.set_status(stated.memory_id, "active", scope=mine, actor_id="engineer-1")
    assert manager.recall("pump sleeves", mine)
    with pytest.raises(ValueError):
        manager.set_status(stated.memory_id, "superseded", scope=mine, actor_id="engineer-1")
    operations = [r["operation"] for r in db.query("SELECT operation FROM memory_events ORDER BY id")]
    assert operations == ["create", "ignore", "edit", "archive", "restore"]
    assert [e["operation"] for e in manager.events(outsider)] == []


def test_a_task_that_ends_again_brings_its_own_outcome_up_to_date(db: Database):
    """After a revision a task ends a second time: its record of how it ended is updated,
    not joined by a near-copy -- while another task's outcome, and the same task's outcome
    in another compartment, are left alone. No model is asked."""
    model = ScriptedModel()
    manager = _manager(db, model)
    task, other = str(uuid.uuid4()), str(uuid.uuid4())

    def ended(text: str, task_id: str, compartment: Compartment = PE_INTERNAL) -> Any:
        return manager.propose(Candidate(content=text, memory_type="outcome", tier="episodic", subject="Lathe L-1 report"),
                               compartment=compartment, actor_id="engineer-1", task_id=task_id, use_model=False,
                               replaces_same_task=True)

    first = ended("R. Kulkarni ran the task 'Report on lathe L-1'; it ended completed with 'Report on lathe L-1' (v1).", task)
    elsewhere = ended("R. Kulkarni ran the task 'Report on pump P-3'; it ended completed.", other)
    secret = ended("R. Kulkarni ran the task 'Report on lathe L-1'; it ended completed (confidential copy).", task,
                   PE_CONFIDENTIAL)
    again = ended("R. Kulkarni ran the task 'Report on lathe L-1'; it ended completed with 'Report on lathe L-1' (v3).", task)
    assert [first.operation, elsewhere.operation, secret.operation, again.operation] == ["create", "create", "create", "update"]
    assert again.memory_id == first.memory_id and again.target_id == first.memory_id
    assert _row(db, first.memory_id)["content"].endswith("(v3).")
    assert "(v1)" in str(db.scalar("SELECT candidate FROM memory_events WHERE memory_id = %(m)s::uuid AND operation = 'create'",
                                   {"m": first.memory_id}))  # the earlier wording stays in the log
    assert db.scalar("SELECT count(*) FROM memories WHERE status = 'active'") == 3
    assert model.calls == []
