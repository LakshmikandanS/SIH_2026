# Block 02 — Query Router + Model Router

## Role

This block answers two different questions:

```text
Query Router:
"What kind of request is this?"

Model Router:
"Which approved local model is best suited to handle it?"
```

Keeping them separate prevents hard-coding model names into application logic.

## Logical architecture

```text
                 Request
                    │
                    ▼
          ┌───────────────────┐
          │  Query Classifier │
          └─────────┬─────────┘
                    │
       task / query / control
                    │
                    ▼
          ┌───────────────────┐
          │ Capability Router │
          └─────────┬─────────┘
                    │
          vision / code / docs
                    │
                    ▼
          ┌───────────────────┐
          │   Model Router    │
          └─────────┬─────────┘
                    │
        ┌───────────┼────────────┐
        ▼           ▼            ▼
     LLM-A       LLM-B       Vision-OCR
```

## Query classification

Minimum classes:

- `TASK`
- `QUERY`
- `CONTROL`
- `APPROVAL`
- `STATUS`

Task metadata should include:

```text
domain
required_modalities
expected_output
sensitivity
estimated_context_size
latency_budget
quality_requirement
```

## Model capability manifest

Do not write:

```python
model = "qwen-..."
```

throughout the application.

Instead:

```json
{
  "model_id": "local_reasoner_01",
  "provider": "vllm",
  "capabilities": [
    "reasoning",
    "tool_use",
    "long_context"
  ],
  "context_window": 32768,
  "min_vram_gb": 24,
  "quality_tier": 3,
  "approved_domains": [
    "maintenance",
    "document_analysis"
  ],
  "status": "HEALTHY"
}
```

## Routing score

A conceptual score can combine:

```text
score =
 capability_match
 + quality_score
 + locality_score
 + health_score
 - latency_penalty
 - resource_penalty
```

Do not require a complex ML router for the prototype. A deterministic weighted score is enough.

## Example

User submits:

> Analyze these scanned inspection reports and prepare an approval note.

Router infers:

```text
document = true
vision = true
retrieval = true
reasoning = true
output = DOCX
```

Model Router could select:

```text
Vision model → scan extraction
Embedding model → retrieval
Reasoning model → synthesis
```

## Interfaces

### POST /v1/route/query

Input:

```json
{
  "request_id": "req_1",
  "objective": "...",
  "context": {},
  "classification": "CONFIDENTIAL"
}
```

Output:

```json
{
  "intent": "TASK",
  "required_capabilities": [
    "retrieval",
    "vision",
    "reasoning"
  ]
}
```

### POST /v1/route/model

Output:

```json
{
  "model_id": "reasoner_02",
  "endpoint": "http://model-gateway/v1",
  "fallback_model_id": "reasoner_01",
  "reason": "best approved reasoning model under current VRAM budget"
}
```

## Security rules

The router may only choose models registered as:

```text
APPROVED
HEALTHY
LOCAL
POLICY-COMPATIBLE
```

It must reject:
- cloud API endpoints
- unapproved local models
- models incompatible with data classification

## Failure strategy

If best model is unavailable:

```text
model A unavailable
      ↓
fallback model B
      ↓
if B unavailable
      ↓
task PAUSED
```

Never silently route confidential work to the Internet.
