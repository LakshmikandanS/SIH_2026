# AGENTS.md — memory

Three tiers. Working memory is ours; episodic and semantic come from Monarch.

## Depends on

`contracts`, `platform`, `gateway`.

## The tiers

| Tier | Scope | Where it lives |
|---|---|---|
| **Working** | One task. Structured, extensible. Discarded or archived at task end | Here, Postgres |
| **Episodic** | Across tasks: decisions, outcomes, rejections | Monarch, behind the seam |
| **Semantic** | Durable organisational facts: equipment, vendors, conventions, people, house style | Monarch, behind the seam |

Memory writes are deliberate operations with a lifecycle. Never a side effect of a chat
turn.

## Monarch is a dependency, never a merge

Hard constraint. Two repositories, one dependency relationship. Installed as a
**version-pinned package** (it already has `src/` layout, `pyproject.toml`, setuptools,
entry points). Not a submodule. Not vendored source. The built wheel goes into the offline
bundle.

## ⚠️ The seam does not exist yet

The handoff describes it as though it were already there. Reading Monarch's code, it is
not:

- `Memory` (`memory/models.py`) has **no scope, tenant or owner field**.
- `retrieve_memories(query, top_k)` takes **no visibility predicate**. It calls
  `repository.search_similar(...)` — a global search over one store — then filters
  `status == ACTIVE` **in Python, after retrieval**.
- `repository` and `config` are module-level singletons, so there is no per-caller context
  to thread a scope through.

**Monarch post-filters.** Wiring Citadel's memory tiers to it as it stands would import
into the memory path exactly the pattern Citadel refuses in the retrieval path — and
organisational memory is not less sensitive than the document corpus.

So the seam is an **upstream change to Monarch**, not an adapter on this side:

1. A `scope_key` on the memory record — opaque to Monarch, indexed.
2. A visibility predicate accepted by `retrieve_memories` and **pushed into the LanceDB /
   SQLite query**, not applied after.
3. Repository and config injected per call or per session, without which (1) and (2) have
   nowhere to live.

### Until then

**Working memory only.** Episodic and semantic sit behind an interface in this package with
no implementation. M5 needs them; that is the deadline. M0–M4 are not blocked.

Write the interface now, in Citadel's vocabulary, so that when Monarch changes the
integration is an implementation and not a redesign.

## Vocabulary

Monarch's `MemoryType` is person-centric (`USER_PREFERENCE`, `PERSONAL_FACT`,
`TECHNICAL_SKILL`, …), as are `Predicate` and `ObjectType`. Citadel needs document- and
equipment-oriented types.

**The answer is neither "change Monarch's enum" nor "extend on the Citadel side" — it is
to make the vocabulary open.** A closed `StrEnum` that rejects `EQUIPMENT_FACT` is the same
bug as an event vocabulary closed at sixteen. Monarch accepts a registered vocabulary,
Citadel registers its terms at startup, and neither repository learns the other's domain.

Monarch brings SQLite + LanceDB alongside Citadel's Postgres, so the app box runs two
storage engines from M5. That is the price of the seam, and the seam is the point.
