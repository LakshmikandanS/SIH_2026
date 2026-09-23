"""Deterministic, explainable routing (packages/gateway/AGENTS.md "Routing").

A weighted score, no ML router. Every candidate in the active profile's registry is
scored -- eligible or not -- and the whole breakdown travels with the decision, because
the breakdown *is* acceptance target A: the demonstration shows why a model was chosen,
not just which.

Two kinds of criteria, kept apart on purpose:

* **Eligibility** is hard. A model that is not enabled, not installed, cooling down
  after failures, missing a required capability or modality, too small for the
  context, or whose registry classification ceiling is below the data it would see,
  is simply not a candidate. The ceiling check is the same lattice comparison policy
  uses (citadel_contracts.classification), so routing can never send confidential text
  to a model the registry caps lower.
* **Score** is soft and additive: capability coverage, the task's preferred capability,
  quality tier, and residency -- a loaded model scores +15, a model that would have to
  swap in pays a penalty proportional to the swap cost, measured if the registry has a
  measurement and estimated from its size if not (and labelled which). On a profile
  where everything is resident the swap term never appears, and the breakdown still
  reads correctly -- the check that the abstraction is real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from citadel_contracts.classification import Classification
from citadel_platform.registry.schema import ModelEntry

from citadel_gateway.provider import normalise_tag
from citadel_gateway.types import CandidateScore, RoutingDecision, RoutingRequest, ScoreTerm

CAPABILITY_POINTS = 10.0
PREFERRED_POINTS = 25.0
QUALITY_POINTS = {"standard": 10.0, "high": 25.0, "reference": 30.0}
QUALITY_REQUESTED_POINTS = 15.0
RESIDENT_POINTS = 15.0
PINNED_SET_POINTS = 5.0
SWAP_PENALTY_PER_SECOND = 3.0
SWAP_PENALTY_CAP = 30.0
#: Used only when the registry has no measured swap cost: a rough load time per GB of
#: weights, so an unmeasured model is still penalised in proportion to its size. The
#: breakdown labels the figure "estimated" -- ADR-0001's first M1 measurement replaces it.
ESTIMATED_SWAP_SECONDS_PER_GB = 1.5


@dataclass(frozen=True)
class RuntimeView:
    """What the router knows about the runtime right now."""

    reachable: bool
    installed: frozenset[str] = frozenset()
    loaded: frozenset[str] = frozenset()
    all_resident: bool = False
    cooling_down: dict[str, float] = field(default_factory=dict)  # model_id -> seconds left


def _swap_seconds(model: ModelEntry) -> tuple[float, str]:
    if model.swap_cost_s is not None:
        return float(model.swap_cost_s), "measured"
    return round(model.vram_gb * ESTIMATED_SWAP_SECONDS_PER_GB, 1), "estimated from size"


def score_model(model: ModelEntry, request: RoutingRequest, runtime: RuntimeView) -> CandidateScore:
    tag = normalise_tag(model.tag)
    installed = tag in runtime.installed
    resident = runtime.all_resident or tag in runtime.loaded
    ineligible: list[str] = []

    if not model.enabled:
        ineligible.append("not enabled in the registry")
    if not runtime.reachable:
        ineligible.append("inference runtime unreachable")
    elif not installed:
        ineligible.append("not installed on the runtime")
    if model.id in runtime.cooling_down:
        ineligible.append(f"cooling down after failures ({runtime.cooling_down[model.id]:.0f}s left)")
    missing_modalities = sorted(set(request.modalities) - set(model.modalities))
    if missing_modalities:
        ineligible.append(f"lacks modality {', '.join(missing_modalities)}")
    missing_caps = sorted(set(request.required_capabilities) - set(model.capabilities))
    if missing_caps:
        ineligible.append(f"lacks capability {', '.join(missing_caps)}")
    try:
        if Classification.exceeds(request.classification, model.classification_ceiling):
            ineligible.append(
                f"registry ceiling {model.classification_ceiling} is below the data's {request.classification}"
            )
    except ValueError:
        ineligible.append(f"data classification {request.classification!r} is not in the lattice")
    if request.context_estimate > model.context_window:
        ineligible.append(f"context ~{request.context_estimate} exceeds window {model.context_window}")

    terms: list[ScoreTerm] = []
    covered = [c for c in request.required_capabilities if c in model.capabilities]
    if covered:
        terms.append(ScoreTerm("capability", CAPABILITY_POINTS * len(covered), ", ".join(covered)))
    if request.preferred_capability and request.preferred_capability in model.capabilities:
        terms.append(ScoreTerm("task fit", PREFERRED_POINTS, request.preferred_capability))
    terms.append(ScoreTerm("quality", QUALITY_POINTS.get(model.quality_tier, 0.0), model.quality_tier))
    if request.quality == "high" and model.quality_tier in ("high", "reference"):
        terms.append(ScoreTerm("quality requested", QUALITY_REQUESTED_POINTS, "high"))
    if resident:
        terms.append(ScoreTerm("resident", RESIDENT_POINTS, "loaded now, no swap"))
    else:
        seconds, basis = _swap_seconds(model)
        penalty = -min(SWAP_PENALTY_CAP, round(SWAP_PENALTY_PER_SECOND * seconds))
        terms.append(ScoreTerm("swap", penalty, f"~{seconds:g}s to load ({basis})"))
        if request.latency_budget_s is not None and seconds > request.latency_budget_s:
            ineligible.append(f"swap ~{seconds:g}s exceeds latency budget {request.latency_budget_s:g}s")
    if model.resident and not runtime.all_resident:
        terms.append(ScoreTerm("pinned set", PINNED_SET_POINTS, "member of the resident set"))

    total = round(sum(t.points for t in terms), 1)
    return CandidateScore(
        model_id=model.id,
        tag=model.tag,
        eligible=not ineligible,
        total=total,
        terms=tuple(terms),
        ineligible_because=tuple(ineligible),
        resident=resident,
        installed=installed,
    )


def route(models: Sequence[ModelEntry], request: RoutingRequest, runtime: RuntimeView) -> RoutingDecision:
    scored = [score_model(m, request, runtime) for m in models]
    order = {m.id: i for i, m in enumerate(models)}
    eligible = sorted((c for c in scored if c.eligible), key=lambda c: (-c.total, order[c.model_id]))
    by_id = {m.id: m for m in models}

    if not eligible:
        reasons = "; ".join(c.summary() for c in scored) or "the registry lists no models"
        return RoutingDecision(
            request=request,
            selected=None,
            fallback_chain=(),
            candidates=tuple(scored),
            reason=f"no eligible model for {request.purpose}: {reasons}",
        )

    chosen = eligible[0]
    eligible_ids = {c.model_id for c in eligible}
    chain: list[str] = [f for f in by_id[chosen.model_id].fallback if f in eligible_ids and f != chosen.model_id]
    chain += [c.model_id for c in eligible[1:] if c.model_id not in chain]
    runner_up = f"; runner-up {eligible[1].summary()}" if len(eligible) > 1 else ""
    return RoutingDecision(
        request=request,
        selected=chosen.model_id,
        fallback_chain=tuple(chain),
        candidates=tuple(sorted(scored, key=lambda c: (not c.eligible, -c.total, order[c.model_id]))),
        reason=f"selected {chosen.summary()}{runner_up}",
    )


def candidates_for(models: Iterable[ModelEntry], capability: str) -> list[ModelEntry]:
    return [m for m in models if capability in m.capabilities]


def explain(decision: RoutingDecision, model_id: Optional[str] = None) -> str:
    target = model_id or decision.selected
    for candidate in decision.candidates:
        if candidate.model_id == target:
            return candidate.summary()
    return decision.reason


__all__ = ["RuntimeView", "score_model", "route", "candidates_for", "explain"]
