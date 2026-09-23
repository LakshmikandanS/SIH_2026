"""The serving worker seeds the demo corpus only once the approved models are installed.

Seeded earlier, the scanned reports would be ingested without a vision reading and
without embeddings -- and would stay that way, because ingestion never re-runs on its
own. That is the failure a first `citadel` start before `citadel models` would hit."""

from __future__ import annotations

import threading
import types
from typing import Any

import citadel_worker.__main__ as worker_main


class _NoWait(threading.Event):
    """A stop event whose wait() returns at once, so the retry loop runs at test speed."""

    def wait(self, timeout: float | None = None) -> bool:
        return self.is_set()


def test_the_demo_corpus_waits_for_every_approved_model(monkeypatch: Any, capsys: Any):
    inventory = [["vision-doc", "embed-text"], ["embed-text"], []]

    class Gateway:
        def missing_models(self) -> list[Any]:
            return [types.SimpleNamespace(id=i) for i in (inventory.pop(0) if inventory else [])]

    services = types.SimpleNamespace(gateway=Gateway())
    seeded: list[Any] = []

    def fake_seed(s: Any) -> dict[str, Any]:
        seeded.append(s)
        return {"created": ["IR-2026-0147.pdf"]}

    monkeypatch.setattr(worker_main, "seed", fake_seed)

    worker_main.seed_when_models_ready(services, _NoWait())  # type: ignore[arg-type]

    assert seeded == [services] and inventory == []
    out = capsys.readouterr().out
    assert out.count("waits for the approved models") == 1  # said once, not on every retry
    assert "demo corpus: {'created'" in out


def test_a_stopped_worker_never_seeds(monkeypatch: Any):
    class Gateway:
        def missing_models(self) -> list[Any]:
            return [types.SimpleNamespace(id="embed-text")]

    seeded: list[Any] = []
    monkeypatch.setattr(worker_main, "seed", lambda s: seeded.append(s))
    stop = _NoWait()
    stop.set()
    worker_main.seed_when_models_ready(types.SimpleNamespace(gateway=Gateway()), stop)  # type: ignore[arg-type]
    assert seeded == []
