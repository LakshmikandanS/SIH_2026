# AGENTS.md — gateway

The Model Gateway. Owns the model registry, health, routing, residency, and the
`InferenceProvider` implementations. Nothing else in the repo may talk to an inference
runtime.

## Depends on

`contracts`, `platform`, and an inference client. **It is the only package permitted to
import an inference client or reference an inference host** — enforced by
`tests/structural/test_inference_isolation.py`.

## Routing

Input: `{required_capabilities, modalities, classification, context_estimate,
latency_budget, quality_requirement}`.
Output: a selection, a fallback chain, and **a score breakdown for every candidate**.

The breakdown is not telemetry and not decoration. **It is acceptance target A.** The demo
shows *why* a model was chosen, so the breakdown must survive into the response, into the
trace, and into a purpose-built UI surface.

A weighted score is enough. No ML router. Deterministic and explainable beats clever.

### Residency is a scored term

A resident model scores higher than one requiring a swap, and the estimated swap cost
appears in the breakdown:

```
qwen-general    capability +40  quality +25  resident ✓ +15   = 80
qwen-coder      capability +45  quality +35  swap ~8s   −30   = 50
```

This is what turns the handoff's single biggest technical risk into its best evidence: a
router visibly reasoning about physical constraints is more convincing than one choosing
between two models that both happened to be loaded. On the `hpc-eval` profile the swap term
is always zero and the breakdown still reads correctly — that is the check that the
abstraction is real.

## Residency management

Lives here and nowhere else.

- **`demo-local` / Ollama:** Ollama already implements an admission queue — max loaded models,
  queue-when-full, no CPU spill, keep-alive pinning. **Drive it, do not duplicate it.**
  Read `/api/ps` for truth about what is loaded; set `keep_alive` per model class to pin
  the resident set. A second scheduler fighting Ollama's will thrash.
- **`hpc-eval` / vLLM:** everything is resident. The residency query still answers honestly;
  it just always says yes.

## Health and fallback

Real health probing and a real fallback chain. `fallback_chain = []` and "no health check"
were deliberate design in the prototype and are a named failure mode here.

**A degraded model is never silently used, and a substituted model is never hidden behind a
logical id.** The prototype swapped `hermes3` in behind another model's name when the first
returned empty responses under `format=json`. If the gateway falls back, the response says
so, the trace says so, and the UI says so.

## The provider interface

Written before either implementation. Must carry: streaming, JSON-schema structured output,
a residency query, load/pin/evict (or a documented no-op), per-call model override, and
token accounting on every response. Where a runtime cannot honour one, `supports()` says
so — the gateway records a degraded provider rather than working around it silently.
