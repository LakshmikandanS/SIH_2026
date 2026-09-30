"""Hash-chain strategies for the event log.

Design doc section 6.12 states the formula as
`hash = SHA256(payload + previous_hash)`; `08_OBSERVABILITY.md` gives the same
formula prefixed with "Conceptually"; and the audit's BB-035 says outright
that "the hash formula itself is fully specified and requires no
clarification" -- the black box there was *ordering under concurrency*, which
section 6.12 closes with the single serializing writer, not the formula.

So the formula is settled and this module does not relitigate it. What it does
do is make the exact preimage a named, swappable strategy, because the two
reasonable readings of "payload" differ in what they protect:

  * `payload_only_v1`  -- the doc read literally: SHA256(payload || prev).
    Faithful, but `event_type` sits outside `payload`, so the chain does not
    bind it. The MVP's whole demo turns on TOOL_DENIED vs TOOL_EXECUTED being
    trustworthy, and under this strategy those are interchangeable without
    breaking the chain.

  * `canonical_record_v1` (default) -- SHA256(canonical(record) || prev),
    where the record is the event's own content fields: event_id, task_id,
    actor_id, event_type, payload, timestamp. This is a strict superset of the
    literal formula (payload is still in the preimage, still concatenated with
    previous_hash) and it binds the fields the demo's claims rest on.

Selected by `CITADEL_EVENT_HASH_STRATEGY`. The strategy name is not stored on
the row: the MVP has one chain with one strategy, and persisting a per-row
algorithm tag would be speculative schema. Add that column when a second
strategy actually has to coexist.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable, Mapping

# A strategy takes the event's content fields plus the predecessor's hash and
# returns a 64-char lowercase hex digest.
HashStrategy = Callable[[Mapping[str, Any], str], str]

_REGISTRY: dict[str, HashStrategy] = {}


def register(name: str) -> Callable[[HashStrategy], HashStrategy]:
    def _wrap(fn: HashStrategy) -> HashStrategy:
        _REGISTRY[name] = fn
        return fn

    return _wrap


def get_strategy(name: str) -> HashStrategy:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown event hash strategy {name!r}; available: {sorted(_REGISTRY)}"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)


def _canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, datetimes as
    ISO-8601. Two runs over equal content must produce identical bytes or the
    chain is not verifiable."""

    def _default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"event content is not JSON-serializable: {type(obj).__name__}")

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


@register("payload_only_v1")
def payload_only_v1(record: Mapping[str, Any], previous_hash: str) -> str:
    """SHA256(payload || previous_hash) -- section 6.12 read literally."""
    preimage = _canonical_json(record.get("payload", {})) + previous_hash
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@register("canonical_record_v1")
def canonical_record_v1(record: Mapping[str, Any], previous_hash: str) -> str:
    """SHA256(canonical(event content) || previous_hash) -- the default.

    Binds event_id, task_id, actor_id, event_type, payload and timestamp, so
    relabelling a TOOL_DENIED as a TOOL_EXECUTED breaks the chain.
    """
    content = {
        "event_id": record.get("event_id"),
        "task_id": record.get("task_id"),
        "actor_id": record.get("actor_id"),
        "event_type": record.get("event_type"),
        "payload": record.get("payload", {}),
        "timestamp": record.get("timestamp"),
    }
    preimage = _canonical_json(content) + previous_hash
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()
