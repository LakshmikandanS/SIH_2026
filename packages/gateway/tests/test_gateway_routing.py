"""Routing, fallback, structured output, embeddings, admission and provisioning --
against a real HTTP server speaking the Ollama API (tests/fakes/fake_ollama.py)."""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

import pytest

from citadel_gateway import (
    Gateway,
    Message,
    NoEligibleModel,
    ProviderError,
    RoutingRequest,
    RuntimeView,
    route,
)
from citadel_gateway.ollama import OllamaProvider
from citadel_platform.registry import Registry, load_registry
from citadel_platform.registry.schema import ModelEntry
from fake_ollama import FakeOllama, minimal_instance

REPO_ROOT = Path(__file__).resolve().parents[3]


def _model(model_id: str, tag: str, caps: list[str], **overrides: Any) -> ModelEntry:
    base: dict[str, Any] = {
        "id": model_id,
        "enabled": True,
        "tag": tag,
        "runtime": "ollama",
        "modalities": ["text"],
        "capabilities": caps,
        "context_window": 32768,
        "vram_gb": 2.0,
        "quality_tier": "standard",
        "classification_ceiling": "confidential",
        "resident": True,
        "fallback": [],
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


GENERAL = _model("general", "gen:4b", ["reasoning", "planning", "tool_calling", "structured_output", "drafting"], fallback=["large"])
CODER = _model("coder", "code:3b", ["code_generation", "tool_calling", "structured_output"], fallback=["general"])
LARGE = _model("large", "gen:7b", ["reasoning", "planning", "tool_calling", "structured_output", "drafting"], quality_tier="high", resident=False, vram_gb=4.4)
VISION = _model("vision", "see:2b", ["vision"], modalities=["text", "image"], resident=False)
EMBED = _model("embedder", "embed:1", ["embedding"])
MODELS = (GENERAL, CODER, LARGE, VISION, EMBED)
ALL_TAGS = frozenset(f"{m.tag}" for m in MODELS)


def _registry(models: Sequence[ModelEntry] = MODELS) -> Registry:
    real = load_registry("demo-local", REPO_ROOT / "registry")
    return dataclasses.replace(real, models=tuple(models))


def _view(**kwargs: Any) -> RuntimeView:
    defaults: dict[str, Any] = {"reachable": True, "installed": ALL_TAGS, "loaded": frozenset({GENERAL.tag, CODER.tag})}
    defaults.update(kwargs)
    return RuntimeView(**defaults)


ACT = ("tool_calling", "structured_output")


# -- pure routing -------------------------------------------------------------------


def test_code_task_and_document_task_route_to_different_models():
    """Acceptance target A in miniature: same step kind, two task types, two models."""
    code = route(MODELS, RoutingRequest("act", ACT, preferred_capability="code_generation"), _view())
    doc = route(MODELS, RoutingRequest("act", ACT, preferred_capability="drafting"), _view())
    assert code.selected == "coder"
    assert doc.selected == "general"
    assert "task fit" in code.reason and "code_generation" in code.reason


def test_every_candidate_carries_a_breakdown_even_when_ineligible():
    decision = route(MODELS, RoutingRequest("plan", ("planning", "structured_output")), _view())
    by_id = {c.model_id: c for c in decision.candidates}
    assert set(by_id) == {m.id for m in MODELS}
    assert not by_id["coder"].eligible and "lacks capability planning" in by_id["coder"].ineligible_because
    assert by_id["general"].terms and by_id["large"].terms


def test_resident_model_beats_a_higher_quality_model_that_must_swap():
    decision = route(MODELS, RoutingRequest("plan", ("planning", "structured_output"), quality="high"), _view())
    assert decision.selected == "general"
    large = next(c for c in decision.candidates if c.model_id == "large")
    swap = next(t for t in large.terms if t.term == "swap")
    assert swap.points < 0 and "estimated from size" in swap.detail


def test_high_quality_model_wins_once_it_is_resident():
    view = _view(loaded=frozenset({GENERAL.tag, LARGE.tag}))
    decision = route(MODELS, RoutingRequest("plan", ("planning", "structured_output"), quality="high"), view)
    assert decision.selected == "large"


def test_classification_ceiling_is_a_hard_gate():
    capped = GENERAL.model_copy(update={"classification_ceiling": "INTERNAL"})
    decision = route((capped, LARGE), RoutingRequest("plan", ("planning",), classification="CONFIDENTIAL"), _view())
    general = next(c for c in decision.candidates if c.model_id == "general")
    assert not general.eligible
    assert any("below the data's CONFIDENTIAL" in r for r in general.ineligible_because)
    assert decision.selected == "large"


def test_uninstalled_disabled_and_unreachable_models_are_ineligible():
    view = _view(installed=ALL_TAGS - {CODER.tag})
    decision = route(MODELS, RoutingRequest("act", ACT, preferred_capability="code_generation"), view)
    assert decision.selected == "general"
    disabled = CODER.model_copy(update={"enabled": False})
    assert route((disabled,), RoutingRequest("act", ACT), _view()).selected is None
    down = route(MODELS, RoutingRequest("act", ACT), RuntimeView(reachable=False))
    assert down.selected is None and "unreachable" in down.reason


def test_latency_budget_excludes_a_model_that_would_have_to_swap():
    decision = route((LARGE,), RoutingRequest("plan", ("planning",), latency_budget_s=2.0), _view())
    assert decision.selected is None
    assert "exceeds latency budget" in decision.reason


def test_fallback_chain_follows_the_registry_then_score():
    decision = route(MODELS, RoutingRequest("act", ACT, preferred_capability="code_generation"), _view())
    assert decision.fallback_chain[0] == "general"


def test_all_resident_profile_has_no_swap_term():
    decision = route(MODELS, RoutingRequest("plan", ("planning",)), _view(loaded=frozenset(), all_resident=True))
    for candidate in decision.candidates:
        assert all(t.term != "swap" for t in candidate.terms)


def test_vision_routes_by_modality():
    decision = route(MODELS, RoutingRequest("vision", ("vision",), modalities=("text", "image")), _view())
    assert decision.selected == "vision"
    text_only = route(MODELS, RoutingRequest("vision", ("vision",), modalities=("text", "image")), _view(installed=ALL_TAGS - {VISION.tag}))
    assert text_only.selected is None


# -- the gateway over real HTTP -----------------------------------------------------------


@pytest.fixture()
def fake() -> Iterator[FakeOllama]:
    server = FakeOllama(ALL_TAGS, vision_tags=[VISION.tag]).start()
    try:
        yield server
    finally:
        server.stop()


def _gateway(fake: FakeOllama, models: Sequence[ModelEntry] = MODELS) -> Gateway:
    return Gateway(_registry(models), OllamaProvider(fake.url))


SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string", "minLength": 1}}}


def test_structured_generation_returns_parsed_data_and_the_routing(fake: FakeOllama):
    gateway = _gateway(fake)
    result = gateway.generate(RoutingRequest("act", ACT, preferred_capability="drafting"), [Message("user", "hi")], schema=SCHEMA)
    assert result.data == {"answer": "x"}
    assert result.model_id == "general" and not result.fallback_used
    assert result.routing.selected == "general"
    sent = [body for path, body in fake.requests if path == "/api/chat"][-1]
    assert sent["format"] == SCHEMA and sent["keep_alive"] == "-1"


def test_a_failing_model_falls_back_and_says_so(fake: FakeOllama):
    fake.fail_next[CODER.tag] = "CUDA out of memory"
    gateway = _gateway(fake)
    result = gateway.generate(RoutingRequest("act", ACT, preferred_capability="code_generation"), [Message("user", "hi")], schema=SCHEMA)
    assert result.model_id == "general"
    assert result.fallback_used
    assert result.attempts[0]["model_id"] == "coder" and not result.attempts[0]["ok"]
    assert any("out of memory" in d for d in result.degraded)


def test_invalid_structured_output_is_retried_then_abandoned_honestly(fake: FakeOllama):
    calls: list[str] = []

    def stubborn(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
        calls.append(model)
        if model == GENERAL.tag:
            return "I'd rather not answer in JSON."
        return json.dumps(minimal_instance(schema or {}))

    fake.brain = stubborn
    gateway = _gateway(fake)
    result = gateway.generate(RoutingRequest("act", ACT, preferred_capability="drafting"), [Message("user", "hi")], schema=SCHEMA)
    assert calls[:2] == [GENERAL.tag, GENERAL.tag]  # one stricter retry on the same model
    assert result.fallback_used and result.model_id != "general"
    assert result.data == {"answer": "x"}


def test_no_eligible_model_raises_with_the_decision(fake: FakeOllama):
    gateway = _gateway(fake, models=(CODER,))
    with pytest.raises(NoEligibleModel) as caught:
        gateway.generate(RoutingRequest("plan", ("planning",)), [Message("user", "hi")])
    assert caught.value.decision.selected is None


def test_embeddings_have_the_runtime_dimension(fake: FakeOllama):
    result = _gateway(fake).embed(["pressure vessel", "heat exchanger"], classification="INTERNAL")
    assert result.dimensions == 768 and len(result.vectors) == 2
    assert result.model_id == "embedder"


def test_stream_yields_tokens_then_a_result(fake: FakeOllama):
    gateway = _gateway(fake)
    pieces = list(gateway.stream(RoutingRequest("draft", ("drafting",)), [Message("user", "say something")]))
    assert pieces[-1][1] is not None
    assert "".join(p for p, _ in pieces[:-1]) == pieces[-1][1].text


def test_status_and_pull(fake: FakeOllama):
    fake.installed.discard(f"{LARGE.tag}")
    gateway = _gateway(fake)
    status = gateway.status()
    assert status["reachable"] and status["version"] == "0.0.0-fake"
    assert {m["id"]: m["installed"] for m in status["models"]}["large"] is False
    assert gateway.pull() == ["large"]
    for _ in range(100):
        state = gateway.status()["models"]
        if next(m for m in state if m["id"] == "large")["installed"]:
            break
        threading.Event().wait(0.05)
    assert next(m for m in gateway.status()["models"] if m["id"] == "large")["installed"]


def test_admission_measures_queue_wait(fake: FakeOllama):
    fake.latency_s = 0.3
    registry = _registry()
    registry = dataclasses.replace(registry, profile=registry.profile.model_copy(update={"gpu_admission": 1}))
    gateway = Gateway(registry, OllamaProvider(fake.url))
    waits: list[int] = []
    notified: list[int] = []

    def call() -> None:
        result = gateway.generate(RoutingRequest("act", ACT), [Message("user", "hi")], on_wait=notified.append)
        waits.append(result.queue_wait_ms)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(waits) >= 200
    assert notified == [1]


def test_unreachable_runtime_is_reported_not_raised():
    gateway = Gateway(_registry(), OllamaProvider("http://127.0.0.1:9"))
    status = gateway.status()
    assert status["reachable"] is False and status["error"]
    with pytest.raises(NoEligibleModel):
        gateway.generate(RoutingRequest("act", ACT), [Message("user", "hi")])
    with pytest.raises(ProviderError):
        OllamaProvider("http://127.0.0.1:9").installed()


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def test_an_ambient_proxy_variable_is_never_honoured(fake: FakeOllama, monkeypatch: pytest.MonkeyPatch):
    """The inference endpoint is on-box. A proxy variable in the environment -- Docker
    Desktop injects one into every container once a proxy is configured -- must not get
    to carry prompts and document excerpts off it. The proxy here is a dead port: had it
    been honoured, the call could not have reached the runtime at all."""
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    for var in _PROXY_VARS:
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    tags = {model.tag for model in OllamaProvider(fake.url).installed()}
    assert VISION.tag in tags
