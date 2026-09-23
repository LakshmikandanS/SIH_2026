# AGENTS.md — registry

Configuration as **data**. Nothing here is code, and nothing here has a counterpart
`if` statement in the source tree.

## The rule

If a change would add a branch on a model name, a tool name, a department, a document type
or an event type, it belongs in a file here instead. This is invariant 1 and 7 in the root
`AGENTS.md` and it is enforced by `tests/structural/test_no_hardcoded_models.py`.

The prototype's model manifest had two entries and one candidate per capability, and the
test suite asserted that. Adding vision meant breaking a test that existed to stop you.
That is the failure this directory prevents.

## Files

| File | Holds |
|---|---|
| `models.demo-local.yaml` | Model registry for the RTX 5060 demonstration profile |
| `models.hpc-eval.yaml` | Model registry for the university HPC evaluation profile |
| `tools.yaml` | Tool manifest: schema, capabilities, side-effect class, classification ceiling |
| `policy.yaml` | Policy rules. Ordered, first match wins, default deny |
| `roles.yaml` | Role -> capability grants, read by the policy evaluator's `actor.capabilities` |
| `events.yaml` | Event vocabulary. **Open** — registration, not a closed set |
| `templates.yaml` | Deliverable templates and their declared placeholders |
| `profiles.yaml` | Which files each `CITADEL_PROFILE` loads, plus GPU admission width |

## Validation is strict

An unknown field is an error, not a warning. A registry that silently ignores a typo will
cost someone a day, and the error will surface as a routing decision nobody can explain.

## Models are not pulled automatically

Entries carry `enabled: false` until a human approves the download by name, quantisation
and VRAM cost. Fahim has asked to approve these explicitly. An agent proposing a model
proposes it in a message and waits; it does not add `enabled: true` and run a pull.

The approval is recorded where the entry is: `models.demo-local.yaml`'s header names who
approved which models, when, and at what size, and each enabled entry points back to it.
Enabling is still not pulling -- a person starts the download (the "Pull missing models"
button, or `ollama pull`), and the gateway never pulls on its own.
