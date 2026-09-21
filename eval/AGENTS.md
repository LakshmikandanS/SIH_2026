# AGENTS.md — eval

The evaluation harness. **Built at M2, not at the end.**

Handoff §8.3 is explicit about the timing and it is right: an eval harness written after
the system is what produces a number nobody trusts, because there is no history to compare
it against and no build that ever failed on it.

## What it reports

| Metric | Serves |
|---|---|
| Routing accuracy — labelled task type vs selected model, as a confusion matrix | **Target A, directly** |
| Retrieval quality — recall@k, MRR against a labelled query/document set | |
| Citation precision — proportion of citations that actually support the claim | |
| OCR / extraction accuracy — CER and field-level F1 on a held-out labelled sample | |
| Deliverable verification pass rate, **by tier** | So you can see which tier fails |
| Grounding rate — quantitative claims traceable to a cited source | |
| End-to-end task success — rubric-graded, ≥20 realistic tasks | |
| Latency budget adherence per step type, plus measured model swap cost | |

## Golden-set regression

Fixed inputs. Expected output **structure**, never exact text — a generative system that
must reproduce a string is a system you cannot improve. Runs in CI. A drop in any metric
fails the build.

## Run it on both profiles

`demo-local` and `hpc-eval` (ADR-0002, ADR-0003). The interesting comparison is not which is faster — it
is whether routing decisions and retrieval quality are the *same*. If they differ, the
profile abstraction is leaking and that is a bug worth finding before the demonstration.
