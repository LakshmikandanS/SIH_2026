"""The Model Gateway: the only way anything in Citadel reaches a model.

Owns routing, residency, health, fallback and admission for the active profile, and
records every call twice, for two different readers:

* an **audit** event (`model.routed`, plus `model.fallback_used` / `model.degraded`
  when they happen) carrying the governance facts -- which model saw data of which
  classification, for which purpose, on whose behalf, and why that model;
* a **trace** span (kind `model`) carrying the operational facts -- tokens, latency,
  queue wait, and the full score breakdown the routing panel renders.

A degraded model is never silently used and a substituted model is never hidden
behind a logical id: if the first choice fails or returns output that does not match
the requested schema, the result says so (`fallback_used`, `degraded`, `attempts`),
the audit chain says so, and the UI says so.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from citadel_platform.audit.log import AuditLog
from citadel_platform.registry import Registry
from citadel_platform.registry.schema import (
    AllResidentResidency,
    ModelEntry,
    OllamaInference,
    RuntimeManagedResidency,
    SlurmInference,
)
from citadel_platform.tracing import Tracer

from citadel_gateway.admission import AdmissionGate
from citadel_gateway.jsonutil import StructuredOutputError, parse_structured, strip_reasoning
from citadel_gateway.ollama import OllamaProvider
from citadel_gateway.provider import InferenceProvider, normalise_tag
from citadel_gateway.router import RuntimeView, route
from citadel_gateway.types import (
    EmbeddingResult,
    GenerationResult,
    Message,
    NoEligibleModel,
    ProviderError,
    RoutingDecision,
    RoutingRequest,
    Usage,
)
from citadel_gateway.vllm import VLLMProvider

ENDPOINT_OVERRIDE_VAR = "CITADEL_INFERENCE_ENDPOINT"

_INVENTORY_TTL_S = 3.0
_FAILURE_WINDOW_S = 120.0
_COOLDOWN_S = 60.0
_EMBED_BATCH = 32


def build_provider(registry: Registry, env: Optional[Mapping[str, str]] = None) -> tuple[InferenceProvider, dict[str, Any]]:
    """The one place a profile's runtime becomes a provider object. The only
    profile-shaped branch in the codebase, as ADR-0002 requires: on the inference
    config's declared provider, never on the profile's name."""
    environ = env if env is not None else os.environ
    inference = registry.profile.inference
    notes: dict[str, Any] = {}
    override = environ.get(ENDPOINT_OVERRIDE_VAR)
    if isinstance(inference, OllamaInference):
        endpoint = override or inference.endpoint
        if override:
            notes["endpoint_override"] = f"{ENDPOINT_OVERRIDE_VAR} replaces the registry endpoint {inference.endpoint}"
        return OllamaProvider(endpoint), notes
    if isinstance(inference, SlurmInference):
        slurm_endpoint: Optional[str] = override
        if not slurm_endpoint:
            path = Path(os.path.expandvars(inference.endpoint_file))
            if not path.is_file():
                raise ProviderError(
                    f"hpc-eval endpoint file {path} does not exist yet -- the SLURM job writes it "
                    f"once a node is allocated (ops/hpc/README.md)",
                    retryable=False,
                )
            slurm_endpoint = path.read_text(encoding="utf-8").strip()
        return VLLMProvider(slurm_endpoint), notes
    raise ProviderError(f"no provider for inference config {type(inference).__name__}", retryable=False)


class Gateway:
    def __init__(
        self,
        registry: Registry,
        provider: InferenceProvider,
        *,
        audit: Optional[AuditLog] = None,
        tracer: Optional[Tracer] = None,
        notes: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self._audit = audit
        self._tracer = tracer or Tracer(None)
        self._notes = dict(notes or {})
        self.gpu = AdmissionGate("gpu", registry.profile.gpu_admission)
        self._lock = threading.Lock()
        self._view: Optional[RuntimeView] = None
        self._view_at = 0.0
        self._version: Optional[str] = None
        self._last_error: Optional[str] = None
        self._failures: dict[str, list[float]] = {}
        self._pulls: dict[str, dict[str, Any]] = {}

    # -- runtime view -------------------------------------------------------------

    @property
    def models(self) -> tuple[ModelEntry, ...]:
        return self.registry.models

    def runtime_view(self, *, force: bool = False) -> RuntimeView:
        with self._lock:
            cached = self._view
            fresh = cached is not None and time.monotonic() - self._view_at < _INVENTORY_TTL_S
        if fresh and not force and cached is not None:
            return self._with_cooldowns(cached)
        try:
            installed = frozenset(m.tag for m in self.provider.installed())
            loaded = frozenset(m.tag for m in self.provider.loaded())
            view = RuntimeView(
                reachable=True,
                installed=installed,
                loaded=loaded,
                all_resident=isinstance(self.registry.profile.residency, AllResidentResidency),
            )
            error = None
            if self._version is None:
                self._version = self.provider.version()
        except ProviderError as exc:
            view = RuntimeView(reachable=False)
            error = str(exc)
        with self._lock:
            self._view = view
            self._view_at = time.monotonic()
            self._last_error = error
        return self._with_cooldowns(view)

    def _with_cooldowns(self, view: RuntimeView) -> RuntimeView:
        now = time.monotonic()
        cooling: dict[str, float] = {}
        with self._lock:
            for model_id, stamps in self._failures.items():
                recent = [t for t in stamps if now - t < _FAILURE_WINDOW_S]
                self._failures[model_id] = recent
                if len(recent) >= 2 and now - recent[-1] < _COOLDOWN_S:
                    cooling[model_id] = _COOLDOWN_S - (now - recent[-1])
        return RuntimeView(view.reachable, view.installed, view.loaded, view.all_resident, cooling)

    def _record_failure(self, model_id: str) -> None:
        with self._lock:
            self._failures.setdefault(model_id, []).append(time.monotonic())
            self._view_at = 0.0  # re-read the runtime before the next decision

    def _keep_alive(self, model: ModelEntry) -> Optional[str]:
        residency = self.registry.profile.residency
        if isinstance(residency, RuntimeManagedResidency):
            return residency.keep_alive_resident if model.resident else residency.keep_alive_transient
        return None

    # -- routing ------------------------------------------------------------------

    def route(self, request: RoutingRequest) -> RoutingDecision:
        return route(self.models, request, self.runtime_view())

    def _audit_event(self, name: str, actor_id: Optional[str], payload: Mapping[str, Any]) -> None:
        if self._audit is not None:
            self._audit.record(name, actor_id=actor_id, payload=payload)

    # -- generation -----------------------------------------------------------------

    def generate(
        self,
        request: RoutingRequest,
        messages: Sequence[Message],
        *,
        schema: Optional[Mapping[str, Any]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        actor_id: Optional[str] = None,
        on_wait: Optional[Callable[[int], None]] = None,
    ) -> GenerationResult:
        decision = self.route(request)
        if decision.selected is None:
            self._audit_event(
                "model.degraded",
                actor_id,
                {"purpose": request.purpose, "task_id": request.task_id, "reason": decision.reason[:500]},
            )
            raise NoEligibleModel(decision)

        order = [decision.selected, *decision.fallback_chain]
        attempts: list[dict[str, Any]] = []
        degraded: list[str] = []
        total_wait = 0
        with self._tracer.span(
            f"model.{request.purpose}", "model", task_id=request.task_id, attributes={"routing": decision.to_dict()}
        ) as span:
            for position, model_id in enumerate(order):
                model = self.registry.model(model_id)
                try:
                    with self.gpu.acquire(on_wait) as waited:
                        total_wait += waited
                        reply = self.provider.chat(
                            model.tag,
                            messages,
                            schema=schema,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            context_window=min(model.context_window, max(4096, request.context_estimate + 2048)),
                            keep_alive=self._keep_alive(model),
                        )
                        data: Any = None
                        text = strip_reasoning(reply.text)
                        if schema is not None:
                            try:
                                data = parse_structured(reply.text, schema)
                            except StructuredOutputError as first:
                                # One stricter retry on the same model before moving on.
                                nudged = [*messages, Message("assistant", reply.text[:2000]), Message(
                                    "user",
                                    f"That reply was not valid: {first}. Reply again with ONLY a JSON object matching the schema.",
                                )]
                                reply = self.provider.chat(
                                    model.tag,
                                    nudged,
                                    schema=schema,
                                    temperature=0.0,
                                    max_tokens=max_tokens,
                                    context_window=min(model.context_window, max(4096, request.context_estimate + 2048)),
                                    keep_alive=self._keep_alive(model),
                                )
                                data = parse_structured(reply.text, schema)
                                degraded.append(f"{model_id}: needed a retry to produce valid structured output ({first})")
                                text = strip_reasoning(reply.text)
                except (ProviderError, StructuredOutputError) as exc:
                    self._record_failure(model_id)
                    attempts.append({"model_id": model_id, "ok": False, "error": str(exc)[:400]})
                    degraded.append(f"{model_id}: {str(exc)[:200]}")
                    self._audit_event(
                        "model.degraded",
                        actor_id,
                        {"purpose": request.purpose, "task_id": request.task_id, "model_id": model_id, "error": str(exc)[:300]},
                    )
                    continue

                attempts.append({"model_id": model_id, "ok": True, "latency_ms": reply.latency_ms})
                result = GenerationResult(
                    text=text,
                    data=data,
                    model_id=model_id,
                    tag=model.tag,
                    routing=decision,
                    usage=reply.usage,
                    latency_ms=reply.latency_ms,
                    queue_wait_ms=total_wait,
                    fallback_used=position > 0,
                    attempts=attempts,
                    degraded=degraded,
                )
                span.set("model_id", model_id)
                span.set("tag", model.tag)
                span.set("purpose", request.purpose)
                span.set("usage", {"prompt": reply.usage.prompt_tokens, "completion": reply.usage.completion_tokens})
                span.set("latency_ms", reply.latency_ms)
                span.set("queue_wait_ms", total_wait)
                span.set("fallback_used", position > 0)
                span.set("degraded", degraded)
                self._audit_event(
                    "model.routed",
                    actor_id,
                    {
                        "purpose": request.purpose,
                        "task_id": request.task_id,
                        "classification": request.classification,
                        "model_id": model_id,
                        "reason": decision.reason[:400],
                    },
                )
                if position > 0:
                    self._audit_event(
                        "model.fallback_used",
                        actor_id,
                        {"purpose": request.purpose, "task_id": request.task_id, "wanted": decision.selected, "used": model_id},
                    )
                return result

            span.set("attempts", attempts)
            raise ProviderError(
                f"every candidate for {request.purpose} failed: "
                + "; ".join(f"{a['model_id']}: {a.get('error')}" for a in attempts)
            )

    def stream(
        self,
        request: RoutingRequest,
        messages: Sequence[Message],
        *,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        actor_id: Optional[str] = None,
    ) -> Iterator[tuple[str, Optional[GenerationResult]]]:
        """Token deltas as they arrive, then a final `("", result)` carrying usage and
        routing -- the handoff's "streaming token output from every generation call"."""
        decision = self.route(request)
        if decision.selected is None:
            raise NoEligibleModel(decision)
        model = self.registry.model(decision.selected)
        with self._tracer.span(f"model.{request.purpose}", "model", task_id=request.task_id) as span:
            with self.gpu.acquire() as waited:
                started = time.perf_counter()
                pieces: list[str] = []
                usage = Usage()
                for delta, final_usage in self.provider.stream_chat(
                    model.tag,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    keep_alive=self._keep_alive(model),
                ):
                    if delta:
                        pieces.append(delta)
                        yield delta, None
                    if final_usage is not None:
                        usage = final_usage
                latency = int((time.perf_counter() - started) * 1000)
            result = GenerationResult(
                text=strip_reasoning("".join(pieces)),
                data=None,
                model_id=model.id,
                tag=model.tag,
                routing=decision,
                usage=usage,
                latency_ms=latency,
                queue_wait_ms=waited,
            )
            span.set("model_id", model.id)
            span.set("usage", {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens})
            self._audit_event(
                "model.routed",
                actor_id,
                {"purpose": request.purpose, "task_id": request.task_id, "classification": request.classification, "model_id": model.id, "reason": decision.reason[:400]},
            )
            yield "", result

    # -- embeddings and vision --------------------------------------------------------

    def embed(
        self,
        texts: Sequence[str],
        *,
        classification: str,
        task_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> EmbeddingResult:
        request = RoutingRequest(
            purpose="embed",
            required_capabilities=("embedding",),
            classification=classification,
            context_estimate=max((len(t) // 3 for t in texts), default=0),
            task_id=task_id,
        )
        decision = self.route(request)
        if decision.selected is None:
            raise NoEligibleModel(decision)
        model = self.registry.model(decision.selected)
        vectors: list[tuple[float, ...]] = []
        started = time.perf_counter()
        waited_total = 0
        with self._tracer.span("model.embed", "model", task_id=task_id, attributes={"model_id": model.id, "count": len(texts)}):
            for offset in range(0, len(texts), _EMBED_BATCH):
                batch = list(texts[offset : offset + _EMBED_BATCH])
                with self.gpu.acquire() as waited:
                    waited_total += waited
                    try:
                        raw, _usage = self.provider.embed(model.tag, batch, keep_alive=self._keep_alive(model))
                    except ProviderError:
                        self._record_failure(model.id)
                        raise
                vectors.extend(tuple(v) for v in raw)
        dimensions = len(vectors[0]) if vectors else 0
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=model.id,
            tag=model.tag,
            dimensions=dimensions,
            latency_ms=int((time.perf_counter() - started) * 1000),
            queue_wait_ms=waited_total,
        )

    def vision(
        self,
        prompt: str,
        images: Sequence[str],
        *,
        classification: str,
        schema: Optional[Mapping[str, Any]] = None,
        purpose: str = "vision",
        task_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        on_wait: Optional[Callable[[int], None]] = None,
    ) -> GenerationResult:
        request = RoutingRequest(
            purpose=purpose,
            required_capabilities=("vision",),
            modalities=("text", "image"),
            classification=classification,
            context_estimate=1500 + 1000 * len(images),
            task_id=task_id,
        )
        return self.generate(
            request,
            [Message("user", prompt, tuple(images))],
            schema=schema,
            temperature=0.1,
            actor_id=actor_id,
            on_wait=on_wait,
        )

    # -- status and provisioning ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        view = self.runtime_view(force=True)
        loaded_detail: dict[str, Any] = {}
        if view.reachable:
            try:
                loaded_detail = {m.tag: m for m in self.provider.loaded()}
            except ProviderError:
                loaded_detail = {}
        models = []
        for model in self.models:
            tag = normalise_tag(model.tag)
            live = loaded_detail.get(tag)
            models.append(
                {
                    "id": model.id,
                    "tag": model.tag,
                    "enabled": model.enabled,
                    "installed": tag in view.installed,
                    "loaded": view.all_resident or tag in view.loaded,
                    "resident_set": model.resident,
                    "capabilities": list(model.capabilities),
                    "modalities": list(model.modalities),
                    "quality_tier": model.quality_tier,
                    "context_window": model.context_window,
                    "registry_estimate_gb": model.vram_gb,
                    "measured_gpu_bytes": getattr(live, "vram_bytes", None),
                    "classification_ceiling": model.classification_ceiling,
                    "cooling_down_s": round(view.cooling_down.get(model.id, 0.0)),
                    "pull": self._pulls.get(model.id),
                }
            )
        return {
            "provider": self.provider.name,
            "endpoint": self.provider.endpoint,
            "reachable": view.reachable,
            "version": self._version,
            "error": self._last_error,
            "notes": self._notes,
            "profile": self.registry.profile.name,
            "residency_strategy": self.registry.profile.residency.strategy,
            "admission": self.gpu.snapshot(),
            "models": models,
        }

    def missing_models(self) -> list[ModelEntry]:
        view = self.runtime_view(force=True)
        return [m for m in self.models if m.enabled and normalise_tag(m.tag) not in view.installed]

    def pull(self, model_ids: Optional[Sequence[str]] = None) -> list[str]:
        """Start pulling enabled-but-missing models in the background; returns which.
        A human starts this (the UI button or the launcher): registry/AGENTS.md's rule
        that models are never pulled automatically."""
        if not self.provider.supports().pull:
            raise ProviderError(f"{self.provider.name} does not support pulling models", retryable=False)
        targets = [m for m in self.missing_models() if model_ids is None or m.id in model_ids]
        started: list[str] = []
        for model in targets:
            state = self._pulls.get(model.id)
            if state and not state.get("done"):
                continue
            self._pulls[model.id] = {"status": "queued", "completed": 0, "total": 0, "done": False, "error": None}
            started.append(model.id)
        if started:
            threading.Thread(target=self._pull_worker, args=(started,), daemon=True, name="model-pull").start()
        return started

    def _pull_worker(self, model_ids: Sequence[str]) -> None:
        for model_id in model_ids:
            model = self.registry.model(model_id)
            state = self._pulls[model_id]

            def progress(update: Mapping[str, Any], state: dict[str, Any] = state) -> None:
                state["status"] = str(update.get("status") or state["status"])
                if isinstance(update.get("total"), int):
                    state["total"] = update["total"]
                if isinstance(update.get("completed"), int):
                    state["completed"] = update["completed"]

            try:
                self.provider.pull(model.tag, progress)
                state["status"] = "installed"
            except ProviderError as exc:
                state["error"] = str(exc)
                state["status"] = "failed"
            finally:
                state["done"] = True
                with self._lock:
                    self._view_at = 0.0

    def warm_resident_set(self) -> list[str]:
        """Load and pin the registry's resident set, so the first real call does not
        pay a cold load. Best effort; a model that fails to load is simply not warm."""
        warmed: list[str] = []
        if not self.provider.supports().pin_evict:
            return warmed
        view = self.runtime_view(force=True)
        for model in self.models:
            if model.enabled and model.resident and normalise_tag(model.tag) in view.installed and "embedding" not in model.capabilities:
                keep = self._keep_alive(model)
                if keep is None:
                    continue
                try:
                    self.provider.set_keep_alive(model.tag, keep)
                    warmed.append(model.id)
                except ProviderError:
                    continue
        return warmed


__all__ = ["Gateway", "build_provider", "ENDPOINT_OVERRIDE_VAR"]
