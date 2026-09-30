# Citadel Technical Black-Box Audit

**Subject:** Sovereign Agentic AI Workbench — Detailed Block-Level Design & Integration Specification
**Material audited:** 15 files (`00_MASTER_ARCHITECTURE.md` through `14_ARCHITECTURE_DECISIONS.md`)
**Audit type:** Specification completeness / implementation-boundary audit
**Auditor stance:** No redesign, no solutions — gap identification only

---

## Executive Summary

- **47 black boxes** identified across **13 architectural areas** (12 named components plus one cross-cutting "architecture-wide" bucket for issues that don't belong to a single block).
- **Severity spread:** 12 Critical, 20 High, 13 Medium, 2 Low.
- **5 direct cross-document contradictions** were found — two documents asserting incompatible things about the same mechanism, not merely gaps.
- **Major concentration areas:** Control Plane (7 findings, 5 Critical) and Data Plane (6 findings) are the two most under-specified blocks relative to how much architectural weight the specification places on them. Execution Plane/Sandbox and Orchestrator each carry 5 findings apiece, several of them Critical.
- **Most consequential gaps:**
  1. There is no document, anywhere, that specifies how an **agent actually reasons or decides** (its execution loop, action representation, or termination condition) — every other component is defined in terms of governing, sandboxing, or logging "the agent," but the agent itself is never designed (BB-001).
  2. The **hand-off from the Query/Model Router to the Orchestrator** — the single step that turns a classified, model-assigned request into a running plan — has no interface contract anywhere in the 15 files (BB-004).
  3. The **Tool Gateway's authorization model is internally contradictory**: one document has it re-checking the Control Plane on every call, another shows a capability token being used directly against it with no further Control Plane round-trip, and the Control Plane's own service inventory never lists a Tool Gateway at all (BB-015, C-002).
  4. **Sandboxed code execution requires creating a new container from inside a container that the isolation rules explicitly deny a Docker socket to** — the specification asserts both things in the same file (BB-016).
  5. **No document states whether Control Plane, Orchestrator, Execution Plane, and Data Plane are separate networked services or modules in one process** — and the entire "agents do not get direct authority" security model depends on that answer (BB-042).
- This summary intentionally omits proposed fixes. Each finding below is traceable to a specific file and section.

---

## How to read this report

- **BB-IDs** are grouped by the component most responsible for the gap, but many findings span two or three files — every source is cited.
- **Severity** grades the specification gap, not the difficulty of the underlying engineering problem.
- Per the audit brief, this report identifies gaps; it does not propose APIs, protocols, algorithms, or architectures to close them.

---

## Main Black-Box Inventory

| ID | Component | Location | Black Box | What Is Currently Specified | What Is Unspecified | Category | Severity |
|----|-----------|----------|-----------|------------------------------|----------------------|----------|----------|
| BB-001 | Agent / Agent Runtime | Absent from all 15 files; implied by 00 §2–3, 03, 04, 09 | Agent internal execution/cognition model | Agents have state, run in sandboxes, call tools via a gateway, and are subject to policy checks | How an agent decides what to do next, represents an "action," knows when a step is complete, or terminates — the agent's own control loop | A, D | Critical |
| BB-002 | Orchestrator | 03 §Role, §Main responsibilities; 00 §7 step 4 | Plan-generation mechanism | Orchestrator "creates plans"; a Plan object schema (steps, required_capabilities) is given | Whether plan creation is a model call, a template, or a search/planning algorithm; what input it consumes; which model or prompt is involved | A, D | Critical |
| BB-003 | Orchestrator | 03 §Role (loop), §Dispatch sequence | Plan revision mechanism | "REVISE" is a named stage in the PLAN→ACT→OBSERVE→REVISE loop | Whether revision is a partial or full replan, which prior steps/evidence are preserved, and what triggers it vs. simply continuing | D | High |
| BB-004 | Query/Model Router ↔ Orchestrator | Gap between 12 §2 and §3; 00 §7 | Missing router-to-orchestrator interface | Contracts exist for CLI→Query Router and Query Router→Model Router; Orchestrator→Control Plane contract exists separately | No contract shows how the Orchestrator receives the classified request + selected models to begin planning at all | B | Critical |
| BB-005 | Query Router | 02 §Query classification, §Logical architecture | Query classification mechanism | Five target classes (TASK/QUERY/CONTROL/APPROVAL/STATUS) and the task-metadata field list are named | How raw input is actually classified into these classes, and how fields like `sensitivity` or `estimated_context_size` are derived pre-processing | A | High |
| BB-006 | Model Router | 02 §Routing score | Routing-score computation | A conceptual formula is given (capability_match + quality_score + locality_score + health_score − latency_penalty − resource_penalty) | Units, ranges, normalization, and weights for every term; how `latency_budget`/`quality_requirement` metadata feed the formula; tie-breaking | A, C | High |
| BB-007 | Model Router | 02 §Model capability manifest vs §Security rules | Manifest lacks classification-compatibility field | Manifest has `approved_domains`; security rules require rejecting "models incompatible with data classification" | No manifest field encodes classification compatibility, so the stated rejection rule has nothing to evaluate against | C, H | High |
| BB-008 | Model Router | 02 §Model capability manifest, §Failure strategy; 01 §/resume | Model health/availability + fallback exhaustion | Manifest has a `status` field; failure strategy shows one `fallback_model_id` before PAUSE | How `status` is computed/updated; what happens after the single named fallback also fails beyond "PAUSED"; what un-pauses the task | F, L | Medium |
| BB-009 | Query Router / Model Router | 02 §Interfaces vs 12 §2 | Route-model input schema missing + conflicting response shape | 02 shows `/v1/route/model` returning one model; 12 shows the same conceptual step returning `selected_models` for three capabilities at once | No input schema for `/v1/route/model` at all; the two documents describe incompatible response shapes for what reads as the same operation | B, C | High |
| BB-010 | Orchestrator / Model Router | 03 §Retry policy; 02 §Failure strategy | Retry/fallback ownership overlap; undefined escalation target | Orchestrator retries "transient model timeout"; Model Router separately runs its own fallback chain on model unavailability | Which component actually owns recovery from a failed model call; what "escalate" means or who/what receives an escalation | D, F | Medium |
| BB-011 | Orchestrator / Data Plane / Sandbox | 03 §Multi-agent pattern; 06 §2 Shared state; 09 §Concurrency | Shared-state merge/conflict-resolution algorithm | Optimistic-concurrency read/compare/commit pseudocode is given in three files; all three say "merge" on conflict | No file defines what "merge" means for the underlying JSON structures (arrays of decisions/evidence/facts) | E, G | Critical |
| BB-012 | Orchestrator | 03 §Checkpointing; 09 §Agent State | Checkpoint object schema/storage | Checkpoint *trigger points* are listed; agent state references a `last_checkpoint` ID string | What a checkpoint actually contains (full snapshot vs. delta) and where/how it is persisted | C, J | Medium |
| BB-013 | Orchestrator / Execution Plane | 03 §Dispatch sequence; 04 §Role; 12 §4 | Container-lifecycle ownership boundary | Orchestrator "spawns" agents per its dispatch diagram; 04 separately describes Execution Plane as "where agents execute" | Whether the Orchestrator itself calls container APIs (arguably privileged work the doc says it shouldn't do) or a distinct Execution Plane service does, and what interface separates them | A, L | High |
| BB-014 | Orchestrator | 03 §Multi-agent pattern | Supervisor-agent identity ambiguity | A "Supervisor" node dispatches to Retrieval/Vision/Report agents in a diagram | Whether Supervisor is the Orchestrator itself or a sandboxed agent subject to its own capability grants like any other agent | A, H | High |
| BB-015 | Control Plane / Execution Plane | 04 §Tool gateway; 05 §Main services; 12 §3, §5, §6 | Tool Gateway ownership + per-call authorization contradiction | 04 draws Agent→Gateway→Control Plane→Tool; 12 shows a capability token issued once and used directly against the gateway; 05's service list omits a Tool Gateway entirely | Which component hosts the Tool Gateway, and whether authorization is re-checked per call or delegated once via token | B, H, L | Critical |
| BB-016 | Execution Plane | 04 §Isolation controls vs §Sandboxed code execution | Nested-container creation vs. no-Docker-socket rule | Isolation controls state agent containers get "no Docker socket"; the code-execution pipeline requires creating a new "one-shot container" per submission | How a container denied Docker access can cause a sibling/nested container to be created | H, I | Critical |
| BB-017 | Execution Plane / Tool Layer | 04 §Tool gateway, §Tool definitions; 09 §Resources | No common interface across heterogeneous tool execution locations | Tools include in-sandbox code execution, MCP servers, and Data-Plane-backed search, all under one `tool_id` schema | No shared invocation/result contract that actually works across these three different execution locations | B, L | High |
| BB-018 | Execution Plane / Sandbox | 04 §Failure recovery; 09 §Crash recovery | Failure-recovery semantics (retry vs. reassign; resume idempotency) | High-level recovery flows are given ("retry or reassign"; "resume next unfinished step") | Criteria for choosing retry vs. reassign; whether a partially-completed, side-effecting step (e.g., an already-executed tool call) is safely re-run or skipped on resume | F, J | High |
| BB-019 | Control Plane | 05 §Policy inputs, §RBAC+ABAC, §Policy decision example | Policy-decision algorithm / conflict resolution | A list of policy inputs and one example input/output decision are given | How RBAC, ABAC, and capability constraints are actually combined into ALLOW/DENY, and how conflicting applicable policies are resolved | A, D | Critical |
| BB-020 | Control Plane | 05 §Capability-based permissions; 12 §3 | Capability-token mechanics | Tokens are described as "opaque, short-lived" and appear in interface examples | Format, issuance process, verification method, expiry duration, and revocation mechanism | B, H | Critical |
| BB-021 | Control Plane | 05 §Emergency controls | Emergency-control enforcement/propagation | Six named emergency actions are listed (KILL TASK, KILL AGENT, DISABLE TOOL, DISABLE MODEL, GLOBAL NETWORK BLOCK, QUARANTINE ARTIFACT) | How any of the six actually propagates to in-flight orchestrator state, live containers, or other services | D, F, L | Critical |
| BB-022 | Control Plane | 05 §Secrets | Secret/credential delivery to agents | "Agents receive short-lived capability-scoped credentials where necessary" | Delivery mechanism (env var, mounted file, per-call injection), scoping granularity, and rotation | H | High |
| BB-023 | Control Plane / CLI | 05 §Main services (Identity Service); 01 §CLI→Query Router headers | Identity/session lifecycle | An Identity Service is named; CLI requests carry `user_id`/`session_id` | How a session is created/expires, and whether `user_id` is verified per request or trusted as claimed | H, J | High |
| BB-024 | CLI | 01 §CLI→Query Router | CLI-to-backend authentication mechanism | An `Authorization` header is recommended | What the header actually contains (bearer token, mTLS, API key) or how it's issued/validated | B, H | High |
| BB-025 | CLI / Orchestrator / Control Plane | 01 §/status, §Command contract; 03 §Orchestrator interfaces | Read-path ownership ambiguity + missing schemas | 01 attributes `/status` to "control/data APIs"; 03 separately defines `GET /tasks/{id}` as an Orchestrator endpoint | Which service actually serves task status; request/response schemas for `/artifacts` and `/trace` (given for `/task` and `/approve` only) | B, C, L | Medium |
| BB-026 | Data Plane | 06 §6 Memory types | Working Memory & Organizational Knowledge have no schema | Four memory types are named and distinguished conceptually; Task memory alone gets a JSON shape (via 09) | No schema, storage backend, or query interface for Working Memory or Organizational Knowledge | C, E | High |
| BB-027 | Data Plane / Local RAG | 06 §6 vs 10 §Pipeline | Organizational Knowledge vs. RAG store relationship | 06 lists "Organizational knowledge" as a memory type; 10 separately describes document ingestion into a vector DB | Whether these are the same store or two different systems | L | Medium |
| BB-028 | Data Plane | 06 §7 Provenance | Provenance record storage/query mechanism | Six fields evidence "should point to" are listed | No schema, storage location, or query interface for the provenance graph itself | C, E | Medium |
| BB-029 | Data Plane | 06 §8 Retention | Retention/purge mechanism | Three retention tiers are named (temporary task data, approved artifacts, audit events) | What triggers a purge; how purging temporary data interacts with a permanently retained, RELEASED artifact whose provenance cites that now-purged data | J, F | High |
| BB-030 | Data Plane | 06 §9 Backup | Cross-store consistent backup/restore | A list of systems needing backup (Postgres, vector DB, object store, etc.) is given | How a mutually consistent point-in-time backup/restore is achieved across three different storage systems; RPO/RTO | J, I | Medium |
| BB-031 | Network Layer | 07 §Network identities, §Zero-egress demonstration | Network-identity attribution mechanism | A logging schema (task_id, agent_id, container_id, etc.) is defined for each connection | How a low-level network event (an nftables rule hit, a DNS query) actually gets tagged with those high-level IDs | L, K | High |
| BB-032 | Network Layer / Control Plane | 07 §Mode B; 12 §9 | Egress-authorization interface for controlled connectivity | Mode B names an "Egress Gateway" and "Allow-list" conceptually | No interface contract for how a component requests or receives an approved external route | B, H | Medium |
| BB-033 | Observability | 08 §Agent trace | Summarization step for agent traces | "Record concise action/decision summaries" instead of hidden chain-of-thought | Whether producing that summary is itself a model call subject to the same capability-grant/policy pipeline as any other model use | L, K | Medium |
| BB-034 | Observability / Local RAG | 08 §Evaluation; 10 §RAG quality checks | Evaluation/verification scoring methodology | Metrics are named (task completion, source grounding, citation correctness, policy compliance, etc.) | How any of these are actually measured — rule-based check, human grading, or model-as-judge | A, K | Medium |
| BB-035 | Observability | 08 §Immutable events | Hash-chain ordering under concurrency | A precise formula is given: `event_n.hash = SHA256(payload + event_(n-1).hash)` | Who serializes writes and computes `event_(n-1)` when Orchestrator, Control Plane, Data Plane, Network Layer, and Execution Plane all emit events concurrently | G, E | High |
| BB-036 | Local RAG | 10 §Pipeline ("Metadata + ACL tagging") | ACL-tagging mechanism at ingestion | The pipeline names "Metadata + ACL tagging" as a stage between OCR and chunking | Who or what assigns `classification`/`acl` values to a newly ingested document | H | Critical |
| BB-037 | Local RAG / Deliverables | 10 §Evidence package vs 11 §Verification | Evidence package drops classification/ACL fields | Chunk metadata includes `classification`/`acl`; the returned evidence package schema includes only `document_id`, `page`, `text`, `version` | No path for classification/ACL data to reach the deliverable-verification step that is supposed to check "classification markings correct" | C | Medium |
| BB-038 | Deliverables | 11 §Verification, §Business rules | Verification algorithm + business-rule configuration | Verification categories (file validity, content, policy, business rules) and one example rule are listed | The actual check mechanism (rule engine vs. model-judged) and where/how business rules are defined or configured per document type | A, C | High |
| BB-039 | Deliverables | 11 §Release | Immutability enforcement for RELEASED artifacts | "Artifact version immutable" is stated as a post-approval property | No mechanism (write-once storage, access control, a sealing flag) is named for how immutability is technically enforced | H, J | Medium |
| BB-040 | Integration Contracts | 12 §6 | Tool Gateway → Data Plane contract left as prose | Sections 1–5 and 7 of the same document each give a concrete JSON request/response example | Section 6 gives only a bullet list ("should enforce...") with no schema, inconsistent with every sibling section | B, C | High |
| BB-041 | Integration Contracts / Model Router | 12 §4; 02 §Model capability manifest | GPU representation mismatch; no contention model | Sandbox creation uses `"gpu": true/false`; the model manifest requires a specific `min_vram_gb` | A boolean can't express a specific VRAM requirement; no scheduling/contention model exists for GPU use across concurrent tasks | C, I | High |
| BB-042 | Cross-cutting | 00 §2, §9; 13 §Suggested repository; 14 ADR-007 | Deployment/process topology undefined | A folder-per-component repository layout is suggested; ADR-007 recommends "one-machine orchestration" for the MVP | Whether Control Plane, Orchestrator, Execution Plane, and Data Plane are separate networked services (real trust boundaries) or modules in one process (no real enforcement boundary) | H, L | Critical |
| BB-043 | Cross-cutting / Deliverables | 00 §7 step 12; 11 (entire document) | "Verifier" has no owning component | The golden path names a "Verifier" step; 11 describes an "Automated verification" pipeline stage | Unlike every other named block (CLI, Router, Orchestrator, Control Plane, Execution Plane, Data Plane, Network, Observability), Verifier has no dedicated document, interface, or component definition | A, L | High |
| BB-044 | Sandbox / Data Plane | 09 §Shared State | Shared-state write access enforced only by convention | "Only orchestrator-approved mutations should update shared state" | No described technical control (e.g., network isolation, credential scoping) that actually prevents an agent process from writing shared state directly | H | Medium |
| BB-045 | Deliverables | 11 §Pipeline | Template-selection mechanism | "Template selection" is named as a pipeline stage | How a template is actually chosen for a given task/output type | A | Low |
| BB-046 | Network Layer | 07 §Zero-egress demonstration | Zero-egress dashboard counter computation | Example counters are shown (attempts, blocked, allowed bytes) | How/where these counters are computed or aggregated | K | Low |
| BB-047 | Control Plane | Absent from 12 (no "Approval" section in a 10-section integration-contracts document); 05 §Human approval; 14 ADR-005 | Approval-decision propagation interface missing | An approver-decision object schema exists; ADR-005 states approval is "a state transition" that "generates a signed/auditable event" | No contract shows how a validated approval decision actually reaches the Data Plane or Orchestrator to flip the artifact/task state — every other major hand-off in the architecture has an Integration Contracts entry; this one does not | B, D | Critical |

---

## Deep Analysis of Each Black Box

### BB-001 — Agent Internal Execution/Cognition Model

**Component:** Agent / Agent Runtime (no owning document)

**Source:** Implied throughout `00_MASTER_ARCHITECTURE.md` §2–3, `03_ORCHESTRATOR.md`, `04_EXECUTION_PLANE_SANDBOX.md`, `09_SANDBOX_DETAILED_STATE.md`. No file names an "Agent Runtime" or "Agent" component with its own design.

**Current specification:** The documents specify everything *around* an agent in detail: it runs in an isolated container (04), it has a state object with `current_goal`, `progress`, `working_memory` (09), it must request capabilities from the Control Plane rather than acting directly (00 §2), and it communicates via a Tool Gateway (04). The Orchestrator's PLAN→ACT→OBSERVE→REVISE loop is described as belonging to the Orchestrator, not the agent.

**Black-box boundary:** The specification stops at "the agent has a goal and working memory and calls tools." It never crosses into how the agent turns a goal into a sequence of tool calls, how it decides a step is finished, or how it decides the overall task is done.

**Missing technical definitions:**
1. What the agent's execution loop actually is (single LLM call per step? ReAct-style reasoning loop? fixed script per plan step?).
2. How an agent's "action" is represented internally and communicated to the Tool Gateway (a JSON action object? a function call? free text parsed downstream?).
3. How an agent determines a step is complete versus needing another iteration, and how many iterations are allowed before it's considered stuck.
4. What relationship, if any, exists between the "agent" described here and the "Orchestrator" that already owns planning, dispatch, and revision — i.e., whether an agent has any autonomy at all or is purely a thin executor for Orchestrator-issued steps.

**Why this is a black box:** Two engineers could reasonably build one of two very different systems from this specification: one where "agents" are essentially stateless functions invoked by the Orchestrator with no internal reasoning of their own, and one where each agent is an independent LLM-driven ReAct loop with its own planning capability that duplicates the Orchestrator's job. Nothing in the 15 files distinguishes between these, yet the choice fundamentally changes where most of the system's complexity, cost, and failure modes live.

---

### BB-002 — Plan-Generation Mechanism

**Component:** Orchestrator

**Source:** `03_ORCHESTRATOR.md` §Role, §Main responsibilities; `00_MASTER_ARCHITECTURE.md` §7 step 4.

**Current specification:** The Orchestrator "creates plans." A Plan object schema is given (`plan_id`, `version`, `steps[]` each with `step_id`, `action`, `required_capabilities`). The golden path names "Orchestrator generates a plan" as step 4 of 17.

**Black-box boundary:** The specification defines the *shape* of a plan precisely but never the *process* that produces it.

**Missing technical definitions:**
1. Whether plan generation is a model inference call (and if so, through which interface — does it go through the Model Router like any other model use, and does it require its own capability grant from the Control Plane?).
2. What input the plan-generation step actually consumes beyond "objective" (does it see RAG evidence first, or plan blind and retrieve later?).
3. How step `action` names (e.g., `"retrieve_reports"`) are chosen from — is there a fixed vocabulary of actions, or can arbitrary strings be generated and later matched against available tools?
4. How `required_capabilities` per step are derived and validated against what capabilities actually exist.

**Why this is a black box:** One engineer could implement planning as a single structured-output LLM call with a fixed action vocabulary; another could implement it as a classical HTN/graph planner with no LLM involvement at all. Both would satisfy the documented Plan schema, but they are entirely different subsystems with different failure modes, cost profiles, and testability.

---

### BB-003 — Plan Revision Mechanism

**Component:** Orchestrator

**Source:** `03_ORCHESTRATOR.md` §Role (loop), §Dispatch sequence ("continue or revise").

**Current specification:** REVISE is named as the fourth stage of the orchestration loop, occurring after OBSERVE and feeding back into ACT.

**Black-box boundary:** The loop diagram asserts revision happens; it does not define what changes during a revision.

**Missing technical definitions:**
1. Whether revision produces a brand-new Plan (new `plan_id`) or a new version of the existing plan (`version` incremented, as shown in the Plan schema).
2. Which prior artifacts survive a revision — completed steps, gathered evidence, agent working memory.
3. What specifically triggers revision versus simply proceeding to the next step (an OBSERVE result that contradicts an assumption? a tool failure? every single step?).
4. Whether a revised plan re-enters the Control Plane's capability-grant flow from scratch or reuses grants issued for the original plan.

**Why this is a black box:** Without a defined trigger and scope, one implementation could treat every OBSERVE as a full replan (expensive, but safe against surprises) while another could revise only on explicit failure signals (cheap, but capable of continuing on stale assumptions) — the specification supports either reading equally.

---

### BB-004 — Missing Query/Model Router → Orchestrator Interface

**Component:** Query Router / Model Router / Orchestrator boundary

**Source:** Gap between `12_INTEGRATION_CONTRACTS.md` §2 ("Query Router → Model Router") and §3 ("Orchestrator → Control Plane"); `00_MASTER_ARCHITECTURE.md` §7.

**Current specification:** `12_INTEGRATION_CONTRACTS.md` documents ten integration points end-to-end from CLI through to Network and Observability. Section 1 covers CLI→Query Router; Section 2 covers Query Router→Model Router. Section 3 jumps directly to Orchestrator→Control Plane, already mid-flight ("every privileged operation").

**Black-box boundary:** The document that exists specifically to enumerate every service boundary skips exactly the boundary where the Orchestrator receives its starting input.

**Missing technical definitions:**
1. What request or event actually starts the Orchestrator working on a task — a direct call from the Query Router, from the Model Router, or from some unnamed component that combines both outputs.
2. What that payload contains (is it the original request-envelope fields plus `selected_models`, or a different shape entirely?).
3. Whether this hand-off is synchronous (the router blocks until the Orchestrator accepts) or asynchronous (an event the Orchestrator picks up later).
4. Who is responsible for creating the `task_id` referenced everywhere downstream — the Query Router (which returns `task_id` to the CLI per §1) or the Orchestrator (whose interfaces include `POST /tasks`, implying it creates the task).

**Why this is a black box:** The same document shows `task_id` being returned to the CLI by the Query Router (§1) *and* shows the Orchestrator exposing `POST /tasks` as if tasks are created there (03 §Orchestrator interfaces). Two engineers implementing the two ends of this pipeline independently, using only these documents, would very likely build incompatible assumptions about who creates the task record and how the other side of the pipe learns about it.

---

### BB-005 — Query Classification Mechanism

**Component:** Query Router

**Source:** `02_QUERY_MODEL_ROUTER.md` §Query classification, §Logical architecture.

**Current specification:** Five target classes are named (TASK/QUERY/CONTROL/APPROVAL/STATUS). A "Query Classifier" box appears first in the logical architecture diagram. A metadata field list (`domain`, `required_modalities`, `expected_output`, `sensitivity`, `estimated_context_size`, `latency_budget`, `quality_requirement`) is given as output.

**Black-box boundary:** The categories and the output shape are specified; the classifier itself is a labeled box with no internals.

**Missing technical definitions:**
1. Whether classification is rule-based (e.g., slash-command prefix matching, since the CLI already sends `intent_hint`), an ML/LLM classifier, or a hybrid.
2. How subjective or derived fields like `sensitivity` and `estimated_context_size` are computed *before* any retrieval or model call has happened.
3. What happens when a request doesn't cleanly fit one of the five classes.
4. Whether classification itself requires a model call, and if so, through which model and under which capability grant.

**Why this is a black box:** Because `intent_hint` already exists in the CLI's request payload (`12_INTEGRATION_CONTRACTS.md` §1), an engineer could reasonably conclude classification is a trivial pass-through of client-supplied hints — or, reading `02`'s "Query Classifier" as a real component, could conclude it independently re-derives intent from raw content regardless of client hints. These produce very different trust models (client-asserted vs. server-verified intent).

---

### BB-006 — Routing-Score Computation

**Component:** Model Router

**Source:** `02_QUERY_MODEL_ROUTER.md` §Routing score.

**Current specification:** A conceptual scoring formula is given: `score = capability_match + quality_score + locality_score + health_score − latency_penalty − resource_penalty`, with an explicit note that "a deterministic weighted score is enough" for the prototype.

**Black-box boundary:** The formula's *shape* (which terms exist, and their sign) is specified. Everything about how each term is computed and combined is not.

**Missing technical definitions:**
1. The numeric range and unit of each term (is `capability_match` binary 0/1, or a fractional overlap score against `required_capabilities`?).
2. The weights applied to each term, and whether they are fixed constants or configurable per deployment/task.
3. How `latency_penalty` relates to the `latency_budget` field already present in task metadata (02 §Query classification) — are they the same measurement expressed two ways, or independent?
4. Tie-breaking behavior when two models produce equal scores.

**Why this is a black box:** "Deterministic weighted score" is a real constraint, but it does not by itself determine an implementation — one engineer could weight `health_score` heavily to bias toward availability, another could weight `quality_score` heavily to bias toward output fidelity, and both would be compliant with everything written. In a system whose whole justification for a Model Router (ADR-002) is surviving model changes without redesigning the orchestration layer, the actual selection behavior is exactly the part left undefined.

---

### BB-007 — Model Manifest Lacks a Classification-Compatibility Field

**Component:** Model Router

**Source:** `02_QUERY_MODEL_ROUTER.md` §Model capability manifest vs. §Security rules.

**Current specification:** The manifest schema includes `approved_domains` (e.g., `"maintenance"`, `"document_analysis"`). Separately, the security rules state the router "must reject... models incompatible with data classification."

**Black-box boundary:** The rule references a concept (classification compatibility) that has no corresponding field anywhere in the object the rule is supposed to evaluate.

**Missing technical definitions:**
1. Whether classification compatibility is meant to be derived from `approved_domains` (implicitly, by convention) or requires a dedicated field such as a maximum-classification-level attribute.
2. How a model's classification eligibility is set, audited, or changed over time.
3. What happens to a request whose classification exceeds every available model's compatibility — is the task PAUSED, denied outright, or escalated?
4. Whether classification compatibility is checked by the Model Router itself or deferred to the Control Plane's policy check that occurs later in the golden path.

**Why this is a black box:** As written, an engineer implementing the manifest schema literally (per the JSON example given) has no field to check against the stated security rule — they would have to invent one, and different inventions (a `max_classification` string vs. a `classification: [...]` allowlist vs. reusing `approved_domains`) are not interchangeable and would need independent validation logic downstream.

---

### BB-008 — Model Health/Availability Determination and Fallback Exhaustion

**Component:** Model Router

**Source:** `02_QUERY_MODEL_ROUTER.md` §Model capability manifest, §Failure strategy; `01_CLI.md` §/resume.

**Current specification:** The manifest carries a `status` field (`"HEALTHY"`). The failure strategy shows: model A unavailable → fallback model B → if B unavailable → task PAUSED. The CLI supports a `/resume` command generically (for any paused task).

**Black-box boundary:** The state values and the two-step fallback chain are named; nothing describes how those states are entered, or how a paused task exits PAUSED for this specific cause.

**Missing technical definitions:**
1. What mechanism updates `status` — an active health-check probe, a passive failure counter from real requests, or manual configuration.
2. Whether more than one fallback is ever attempted (the manifest schema only carries a single `fallback_model_id`, not a chain).
3. What automatically un-pauses a model-unavailability-paused task — does the system re-check availability and auto-resume, or does it strictly require a human `/resume`?
4. Whether a task paused for model unavailability is distinguishable, in state or in the CLI's `/status` output, from a task paused for a policy or human-approval reason.

**Why this is a black box:** A single `fallback_model_id` field structurally forecloses chained fallback even though "if B unavailable" language in the failure strategy seems to anticipate needing more than two models — the two parts of this same document are already in tension about how deep the fallback goes.

---

### BB-009 — Route-Model Input Schema Missing / Conflicting Response Shape

**Component:** Query Router / Model Router

**Source:** `02_QUERY_MODEL_ROUTER.md` §Interfaces (`POST /v1/route/model`) vs. `12_INTEGRATION_CONTRACTS.md` §2 (Query Router → Model Router).

**Current specification:** `02` shows `POST /v1/route/model` returning a single object: `{model_id, endpoint, fallback_model_id, reason}`. `12` shows what reads as the same conceptual step (routing a task's required capabilities to concrete models) returning `{selected_models: {vision: "...", reasoning: "...", embedding: "..."}}` — a map with none of `02`'s fields.

**Black-box boundary:** Both documents describe model selection for a multi-capability task (the running example in both files is the same scan-and-report task needing vision, reasoning, and embedding models), but their described outputs are structurally incompatible.

**Missing technical definitions:**
1. Whether model selection for a multi-capability task is one call returning multiple models, or `02`'s single-model endpoint called once per required capability.
2. If it's the latter, how the three individual responses are aggregated into the `selected_models` shape `12` shows downstream.
3. What request body `POST /v1/route/model` actually expects — `02` shows only the response.
4. Whether `endpoint`, `fallback_model_id`, and `reason` (present in `02`'s output) are dropped, or simply omitted from `12`'s abbreviated example while still existing per-model.

**Why this is a black box:** These are not two levels of abstraction of the same thing — they are two different data shapes for what both documents present as the interface a caller uses to get models assigned to a task. An engineer building the Query Router's client code from `12` and an engineer building the Model Router's server response from `02` would produce code that cannot talk to each other without a translation layer neither document describes.

---

### BB-010 — Retry/Fallback Ownership Overlap; Undefined Escalation Target

**Component:** Orchestrator / Model Router

**Source:** `03_ORCHESTRATOR.md` §Retry policy; `02_QUERY_MODEL_ROUTER.md` §Failure strategy.

**Current specification:** The Orchestrator's retry table says "Transient model timeout → retry" and "Repeated tool failure → escalate." Independently, the Model Router has its own two-step fallback-then-pause behavior for model unavailability.

**Black-box boundary:** Both documents assume they own recovery from a failed model call, and neither references the other's mechanism.

**Missing technical definitions:**
1. Whether a "transient model timeout" is retried by the Orchestrator against the *same* model, or triggers the Model Router's fallback chain to a *different* model, or both in some sequence.
2. How many Orchestrator-level retries occur before the Model Router's fallback is engaged, if at all.
3. What "escalate" concretely does — is there a notification channel, a UI surface, an assigned role that receives escalations?
4. Whether escalation is itself an event type in the Observability event catalog (05 §Audit model lists no `ESCALATED` event type).

**Why this is a black box:** Implemented independently, one engineer's Orchestrator retry loop and another engineer's Model Router fallback chain could easily both fire on the same underlying timeout, each unaware of the other, producing either duplicate model calls or a race on which one's outcome the task actually uses.

---

### BB-011 — Shared-State Merge/Conflict-Resolution Algorithm

**Component:** Orchestrator / Data Plane / Sandbox

**Source:** `03_ORCHESTRATOR.md` §Multi-agent pattern; `06_DATA_PLANE.md` §2 Shared state; `09_SANDBOX_DETAILED_STATE.md` §Concurrency.

**Current specification:** All three files independently describe the same optimistic-concurrency pattern: read a versioned state object, prepare a change, submit a conditional write, and on conflict "re-read → merge/recalculate" (03) or "merge" (06) or rely on "deterministic merge functions for known state types" (09).

**Black-box boundary:** The read/compare/commit mechanics are specified precisely (down to the exact request/response shapes in `12_INTEGRATION_CONTRACTS.md` §7). What happens *inside* a merge, the moment two agents' views of `decisions[]`, `discovered_facts[]`, or `assumptions[]` actually diverge, is never specified in any of the three places that invoke the word.

**Missing technical definitions:**
1. Whether a merge is a structural operation (e.g., array concatenation/deduplication) applied automatically, or requires re-invoking whichever agent/model produced the conflicting change.
2. Whether different fields in the Shared State object (`shared_plan`, `decisions`, `evidence`, `assumptions`) use different merge strategies, given `09` refers to "known state types" implying type-specific handling.
3. What happens if a genuine semantic conflict exists (two agents recorded contradictory `decisions`) rather than a purely additive one.
4. How many merge/retry cycles are attempted before the write is treated as a failure requiring orchestrator-level intervention.

**Why this is a black box:** This term appears three times across three different documents describing three different layers (orchestration logic, the state store itself, and the sandbox's view of state) without ever being defined once. It is one of the most load-bearing undefined terms in the specification, since the entire multi-agent coordination story (03's Supervisor pattern) depends on conflicts being resolved correctly and deterministically.

---

### BB-012 — Checkpoint Object Schema and Storage

**Component:** Orchestrator

**Source:** `03_ORCHESTRATOR.md` §Checkpointing; `09_SANDBOX_DETAILED_STATE.md` §Agent State.

**Current specification:** Checkpoint *trigger points* are listed (after plan creation, after retrieval, after each expensive model call, before artifact publication, before human approval). Agent State carries a `last_checkpoint: "chk_19"` string reference.

**Black-box boundary:** When to checkpoint is specified. What a checkpoint object contains, and where it lives, is not.

**Missing technical definitions:**
1. Whether a checkpoint is a full snapshot of TaskState/AgentState or an incremental delta from the previous checkpoint.
2. Which storage system holds checkpoints (a Postgres table, an object-store blob, something else) — `06_DATA_PLANE.md`'s task-state-store table lists no checkpoint-related columns.
3. What `resume(task_id)` (03 §Checkpointing) actually reads to reconstruct state — the latest checkpoint plus a replay of events since, or the checkpoint alone.
4. How checkpoint retention/pruning works over a long-running task with many checkpoints.

**Why this is a black box:** "Resume without re-running completed expensive steps" is a strong, specific promise, but two engineers could satisfy the stated trigger points with either a heavyweight full-snapshot design or a lightweight event-replay design, with very different storage and correctness properties.

---

### BB-013 — Container-Lifecycle Ownership Boundary

**Component:** Orchestrator / Execution Plane

**Source:** `03_ORCHESTRATOR.md` §Dispatch sequence; `04_EXECUTION_PLANE_SANDBOX.md` §Role; `12_INTEGRATION_CONTRACTS.md` §4.

**Current specification:** The Orchestrator's dispatch sequence shows, after a capability grant, "spawn agent" directly as the next step. `04` separately defines the Execution Plane as "where agents actually execute" and states the sandbox "can act, but it cannot decide what it is allowed to act on." `12` §4 shows an "Orchestrator → Sandbox" payload for container creation.

**Black-box boundary:** Whether "Execution Plane" is a distinct, independently deployable service that owns container lifecycle, or simply the label for containers that the Orchestrator itself creates and manages directly, is never resolved.

**Missing technical definitions:**
1. Whether the Orchestrator process holds direct container-runtime credentials (e.g., a Docker/containerd socket) itself, or calls a separate Execution Plane service's API.
2. If a separate service exists, what its own interface looks like beyond the one-directional "creation request" shown in `12` §4 (there's no shown response, no shown teardown/kill call, no shown health-check call).
3. How this reconciles with `03`'s own instruction that the Orchestrator "should manage work, not directly perform privileged work" — whether creating and resourcing a container counts as privileged work.
4. Who owns the resource-allocation decision (`cpu`, `memory_gb`, `gpu` in the `12` §4 payload) — is it computed by the Orchestrator, or does the Execution Plane have its own admission logic that can override the request?

**Why this is a black box:** This determines whether "Execution Plane" is a real architectural boundary (with its own process, its own credentials, and its own security posture) or just a naming convention for "the part of the Orchestrator that talks to Docker." The stated security model in `00` §2 depends on privileged operations being mediated by a boundary — but this specific privileged operation (container creation with resource grants) is drawn as a direct Orchestrator action.

---

### BB-014 — Supervisor-Agent Identity Ambiguity

**Component:** Orchestrator (multi-agent pattern)

**Source:** `03_ORCHESTRATOR.md` §Multi-agent pattern.

**Current specification:** A diagram shows a "Supervisor" node dispatching to Retrieval, Vision, and Report agents, which write to "Shared Evidence."

**Black-box boundary:** "Supervisor" is drawn as a node in an agent hierarchy, but the document never states whether it *is* the Orchestrator (i.e., just a relabeling of the same non-sandboxed control-flow engine already described earlier in the same file) or is itself an agent — meaning it runs in a sandbox, has its own agent state, and must request capability grants like the Retrieval/Vision/Report agents beneath it.

**Missing technical definitions:**
1. Whether Supervisor is sandboxed at all, given `00`'s "most important rule" applies to agents specifically ("Agents do not get direct authority").
2. If Supervisor is an agent, what capability it requests to be allowed to dispatch sub-tasks to other agents — no such capability type appears anywhere in the tool/capability examples.
3. If Supervisor is simply the Orchestrator, why the diagram uses different terminology and draws it as a peer to the agents it supervises rather than as the external control-flow engine described elsewhere in the same document.
4. How Shared Evidence writes from three separate specialist agents interact with the still-undefined merge mechanism (BB-011).

**Why this is a black box:** If Supervisor is an unsandboxed extension of the Orchestrator, the multi-agent pattern is architecturally identical to single-agent dispatch repeated three times, with no interesting coordination problem to solve technically. If Supervisor is itself a sandboxed agent, it introduces an entirely new capability type (agent-to-agent delegation) that is not defined, permissioned, or audited anywhere else in the specification. These are materially different systems.

---

### BB-015 — Tool Gateway Ownership and Per-Call Authorization Contradiction

**Component:** Control Plane / Execution Plane

**Source:** `04_EXECUTION_PLANE_SANDBOX.md` §Tool gateway; `05_CONTROL_PLANE.md` §Main services; `12_INTEGRATION_CONTRACTS.md` §3, §5, §6.

**Current specification:** `04` draws the call chain as `Agent → MCP/tool gateway → Control Plane → Tool` — i.e., every tool invocation passes through the Control Plane inline. `05`'s own inventory of its "Main services" (Identity, Policy Engine, Capability Registry, Approval Service, Audit/Event Service, Secret/Key Service) does not include a Tool Gateway anywhere. `12` shows a capability token issued once by the Control Plane (§3, Orchestrator→Control Plane) and then used directly by the agent against the Tool Gateway (§5, Agent→Tool Gateway) with no further Control Plane round-trip shown before reaching the Data Plane (§6).

**Black-box boundary:** Two different authorization topologies are drawn — one where every tool call re-enters the Control Plane, one where a single upstream grant is reused directly against the gateway — and the component that would host either version (the Tool Gateway) is not claimed by any of the components that have their own document.

**Missing technical definitions:**
1. Whether the Tool Gateway is part of the Execution Plane, part of the Control Plane, or a distinct fourth component never given its own file.
2. Whether authorization is checked once per plan-step (token-based, per `12`) or once per individual tool call (inline, per `04`).
3. If token-based, how the Tool Gateway independently verifies a token's validity/scope without calling back to the Control Plane on every use — no verification mechanism is specified anywhere (see also BB-020).
4. Whether the Tool Gateway is trusted to enforce policy on its own (as `12` §6's "should enforce: authorization context, ACL filters, scope..." implies) or is required to defer every decision to the Control Plane (as `04`'s diagram implies).

**Why this is a black box:** This is the exact mechanism the entire security architecture rests on (00 §2's "most important rule"), and the specification contains two incompatible descriptions of it without acknowledging the difference, plus omits it from the one document (05) that inventories the Control Plane's actual services.

---

### BB-016 — Nested-Container Creation vs. No-Docker-Socket Isolation Rule

**Component:** Execution Plane

**Source:** `04_EXECUTION_PLANE_SANDBOX.md` §Isolation controls vs. §Sandboxed code execution.

**Current specification:** §Isolation controls lists, as a minimum control applied to every agent container, "no Docker socket" and "no host filesystem mount." A few paragraphs later, §Sandboxed code execution describes: "Agent submits code → Policy check → Create one-shot container → Mount only task inputs → Execute → ... → Destroy container," explicitly instructing that model-generated code must not run "in the orchestrator process itself."

**Black-box boundary:** The document asserts, in the same file, that agent containers cannot access the Docker socket and that code submitted by an agent results in a *new* container being created. It never states what process actually holds the Docker/containerd access needed to create that new container.

**Missing technical definitions:**
1. What process or service performs the "Create one-shot container" step — since it cannot be the agent's own container per the isolation rule, is it the Orchestrator, a separate privileged execution-broker service, or the same ambiguous "Execution Plane" from BB-013?
2. How the agent's code (submitted from inside its own locked-down container) physically reaches whatever component does have container-creation privileges.
3. Whether "one-shot container" creation happens on the same host as the agent's own container, and if so, what prevents this from being an escalation path (a compromised or misbehaving agent triggering arbitrary container creation via whatever channel carries its code submission).
4. How the "Mount only task inputs" step is enforced for a container whose contents (the submitted code) originated from a source explicitly denied filesystem/Docker access.

**Why this is a black box:** This is not a case of two engineers filling a gap differently — it's a case where following one part of the specification as written (no Docker socket, ever, for agent containers) appears to make another part of the same specification (agent-submitted code results in new container creation) impossible to implement without introducing an unspecified privileged intermediary that the isolation section never accounts for.

---

### BB-017 — No Common Interface Across Heterogeneous Tool Execution Locations

**Component:** Execution Plane / Tool Layer

**Source:** `04_EXECUTION_PLANE_SANDBOX.md` §Tool gateway, §Tool definitions; `09_SANDBOX_DETAILED_STATE.md` §Resources.

**Current specification:** A single tool-definition schema is given (`tool_id`, `risk`, `input_types`, `output_types`, `requires_approval`, `network_required`, `allowed_classifications`). The Resources list in `09` groups tools (`internal_search`, `OCR`, `code_execution`, `docx_writer`), MCP servers (`internal_db`, `CAD analyzer`), and models under one "capabilities available to the task" umbrella.

**Black-box boundary:** One schema is asserted to cover things that execute in fundamentally different places: code that runs in a freshly created container (per §Sandboxed code execution), a query that hits the Data Plane over what `12` §6 implies is a network call, and an MCP server that is an entirely separate external process with its own protocol.

**Missing technical definitions:**
1. Whether `tool_id` alone is enough for the gateway to know *how* to invoke a given tool (in-process call, container creation, network RPC, MCP protocol call) or whether that routing logic requires additional undocumented metadata.
2. How results are normalized back to the agent across these different execution paths — a code-execution result includes stdout/stderr/artifacts (04 §Sandboxed code execution) while a search-tool result presumably returns structured data; no common result envelope is defined.
3. How `network_required: false` (in the tool definition example, for `docx_writer`) coexists with default-deny network policy for agent containers, given `docx_writer` still needs to reach wherever the agent's temporary output is stored.
4. How MCP-specific concerns (server discovery, connection lifecycle, MCP tool schemas) map onto the generic `tool_id`/`input_types`/`output_types` schema shown.

**Why this is a black box:** A tool gateway built strictly from the one schema shown would work correctly for at most one of these three execution styles without additional, unwritten routing logic — the schema describes tools uniformly while the actual mechanics of invoking them are anything but uniform.

---

### BB-018 — Agent/Step Failure-Recovery Semantics

**Component:** Execution Plane / Sandbox

**Source:** `04_EXECUTION_PLANE_SANDBOX.md` §Failure recovery; `09_SANDBOX_DETAILED_STATE.md` §Crash recovery.

**Current specification:** `04`: "container failed → capture logs → emit AGENT_FAILED event → persist state → retry or reassign." `09`: "container dies → read last checkpoint → restore task state → recreate agent → resume next unfinished step."

**Black-box boundary:** Both documents describe *that* recovery happens; neither defines the decision rule for retry-vs-reassign, nor the granularity at which "next unfinished step" is determined.

**Missing technical definitions:**
1. What criteria distinguish a case warranting "retry" (recreate the same agent, presumably from checkpoint) from one warranting "reassign" (to what — a different agent instance, a different agent type, a human?).
2. Whether "resume next unfinished step" means the step in progress at crash time is re-run from its beginning, or whether partial progress within a step is itself checkpointed.
3. Whether tool calls that already executed and had side effects (e.g., a `docx_writer` call, a state-mutating retrieval marked as read) before the crash are safely idempotent to re-run, or whether the recovery flow can duplicate them.
4. How many retry/reassign cycles are attempted before a task is marked FAILED outright rather than recovered.

**Why this is a black box:** Because checkpointing granularity (BB-012) is itself undefined, "resume next unfinished step" cannot be implemented consistently — an engineer must independently decide both what a checkpoint captures and what counts as a safely-repeatable versus already-committed action, and the specification gives no guidance for either.

---

### BB-019 — Policy-Decision Algorithm / Conflict Resolution

**Component:** Control Plane

**Source:** `05_CONTROL_PLANE.md` §Policy inputs, §RBAC+ABAC, §Policy decision example.

**Current specification:** A list of twelve policy inputs is given (subject, role, department, clearance, task classification, resource classification, requested action, tool risk, model approval status, environment, time/window, approval requirements). RBAC roles are named. One ABAC example expression is given (`department == resource.department AND classification <= clearance`). One example policy request/response pair is shown.

**Black-box boundary:** The specification names every *input* to a policy decision and shows one worked *example* of an output, but never defines the function that turns the twelve inputs into that output in the general case.

**Missing technical definitions:**
1. How RBAC and ABAC layers combine — does an ABAC rule refine an RBAC allow, override it, or do they evaluate independently with a combining rule (e.g., deny-overrides, permit-overrides)?
2. How multiple simultaneously applicable policies (e.g., a department rule and a classification rule) are resolved when they disagree.
3. Which of the twelve listed inputs are mandatory versus optional for a decision, and what the default decision is when an input is missing.
4. How the specific "OPA-style" suggestion in §Implementation maps onto this input list — OPA-style engines evaluate declarative rule sets, but no rule set, rule language, or rule authoring process is described.

**Why this is a black box:** A single worked example establishes that *a* decision can be reached from *some* inputs, but is not sufficient for an engineer to derive the general evaluation algorithm — this is the literal Control Plane example given in the audit brief itself (Example A), and it applies here almost verbatim: the specification states *what* the Control Plane decides without stating *how*.

---

### BB-020 — Capability-Token Mechanics

**Component:** Control Plane

**Source:** `05_CONTROL_PLANE.md` §Capability-based permissions; `12_INTEGRATION_CONTRACTS.md` §3.

**Current specification:** Capability tokens are described narratively as "narrow" (e.g., "agent can read table X where sector=1 and classification <= CONFIDENTIAL"). `12` §3 shows a token appearing in a response as `"capability_token": "opaque-short-lived-token"` and being presented again in §5 (Agent → Tool Gateway) as `"capability_token": "..."`.

**Black-box boundary:** The token's existence, its short lifetime, and its narrow scope are all stated as properties. Nothing about its actual construction or validation is given.

**Missing technical definitions:**
1. The token's format (a signed structured token such as JWT/PASETO, a random opaque string requiring a server-side lookup, or something else).
2. How a downstream verifier (the Tool Gateway, or the Data Plane per `12` §6) validates a token's scope and expiry without necessarily contacting the Control Plane again — or whether it must contact the Control Plane again, which would make "short-lived opaque token" architecturally equivalent to a session reference (relevant to BB-015).
3. How tokens are revoked before their natural expiry (relevant to the "DISABLE TOOL"/"KILL AGENT" emergency controls in BB-021 — a live token for a just-disabled tool would otherwise remain valid until it naturally expires).
4. Whether one token authorizes one specific action (one `RETRIEVE` on one `resource`/`scope`) or a class of actions for the remainder of a plan step.

**Why this is a black box:** The specification treats "capability token" as a solved primitive and reuses it across at least three different integration points without ever defining it once — yet it is the literal mechanism the "most important rule" of the entire architecture (00 §2) depends on for enforcement.

---

### BB-021 — Emergency-Control Enforcement and Propagation

**Component:** Control Plane

**Source:** `05_CONTROL_PLANE.md` §Emergency controls.

**Current specification:** Six named emergency actions are listed: KILL TASK, KILL AGENT, DISABLE TOOL, DISABLE MODEL, GLOBAL NETWORK BLOCK, QUARANTINE ARTIFACT. No further detail is given for any of them.

**Black-box boundary:** The existence of these controls, and an implicit expectation that they exist "for" the Control Plane, is stated. Nothing about how invoking one of them actually takes effect elsewhere in the system is described.

**Missing technical definitions:**
1. For KILL TASK/KILL AGENT: whether this is a graceful signal the Orchestrator/agent must poll for and honor, or a forceful action (e.g., container termination) initiated directly against the Execution Plane — and how in-flight, already-issued capability tokens for that task/agent are invalidated (see BB-020).
2. For DISABLE TOOL/DISABLE MODEL: whether already-issued grants referencing that tool/model remain valid until natural expiry, or are proactively revoked, and how every service holding a cached reference to that tool/model (Model Router health status, Tool Gateway) learns of the change.
3. For GLOBAL NETWORK BLOCK: whether this is implemented at the same enforcement layer as the default-deny egress rules in `07_NETWORK_LAYER.md`, and whether it also blocks internal Data Plane/Postgres/Qdrant traffic that agents legitimately need, or only external egress.
4. For QUARANTINE ARTIFACT: what state an artifact enters (is this a new state alongside TEMP/CANDIDATE/VERIFIED/APPROVED/RELEASED — see the state-machine contradiction, C-001) and whether a quarantined, previously-RELEASED artifact's immutability guarantee (BB-039) is overridden.

**Why this is a black box:** Every one of these six controls is a cross-cutting operation that must be understood and honored by multiple other components (Orchestrator, Execution Plane, Model Router, Network Layer, artifact storage) — yet none of those other components' documents mention receiving or reacting to any of these six signals. The mechanism exists only as a name in one bullet list.

---

### BB-022 — Secret/Credential Delivery to Agents

**Component:** Control Plane

**Source:** `05_CONTROL_PLANE.md` §Secrets.

**Current specification:** "Do not place secrets in prompts, source code, or agent state. Agents receive short-lived capability-scoped credentials where necessary."

**Black-box boundary:** The negative requirement (where secrets must *not* go) is precise. The positive mechanism (how a credential legitimately reaches an isolated, network-restricted agent container) is not described.

**Missing technical definitions:**
1. The delivery channel — an environment variable injected at container-creation time (per the `12` §4 sandbox-creation payload, which currently has no field for this), a mounted file, or an on-demand fetch through the Tool Gateway per use.
2. How "capability-scoped" is technically enforced on the credential itself, versus relying entirely on the separately-described capability token (BB-020) — are these the same mechanism or two different ones?
3. Rotation: whether a credential is single-use, valid for the lifetime of one agent container, or valid for the lifetime of one task.
4. What happens to a credential already delivered to a container if the underlying tool/model is disabled mid-task (see BB-021, DISABLE TOOL).

**Why this is a black box:** "Where necessary" implicitly acknowledges more than one delivery scenario exists, but the document that owns secrets management never enumerates what those scenarios are or how any of them work end-to-end.

---

### BB-023 — Identity/Session Lifecycle and Per-Request Verification

**Component:** Control Plane / CLI

**Source:** `05_CONTROL_PLANE.md` §Main services (Identity Service); `01_CLI.md` §CLI→Query Router headers.

**Current specification:** `05` names an "Identity Service" as one of six Control Plane services, suggesting "internal identity provider/LDAP/AD integration." `01` shows CLI requests carrying `user_id`, `session_id`, and an `Authorization` header, and states the Control Plane "validates whether that user is authorized" for an approval action.

**Black-box boundary:** An Identity Service is named and a login-adjacent header set is recommended; the actual authentication/session flow between them is absent.

**Missing technical definitions:**
1. How a CLI session is initially established (a login command, a pre-provisioned local credential, SSO against the named LDAP/AD integration) — no login command appears in `01`'s command list (`/task`, `/status`, `/approve`, `/resume`, `/artifacts`, `/trace`).
2. Session lifetime and renewal/expiry behavior.
3. Whether `user_id` in a request body is independently verified against the authenticated session on every request, or trusted as client-asserted — this matters directly for the `/approve` flow, where the CLI sends `approver_id` as a plain JSON field alongside (not derived from) the `Authorization` header.
4. Where session state itself is stored (is it part of the "identity" data in Postgres per `06_DATA_PLANE.md`'s task-state store, or a separate store never mentioned).

**Why this is a black box:** The approval-authorization check described in `01`/`05` ("Control Plane validates whether that user is authorized to approve") only means what it's supposed to mean if `approver_id` cannot be spoofed by whoever holds a valid session token for a different user — and nothing in the specification confirms that constraint is enforced rather than merely assumed.

---

### BB-024 — CLI-to-Backend Authentication Mechanism

**Component:** CLI

**Source:** `01_CLI.md` §CLI→Query Router.

**Current specification:** A recommended header set is given: `Authorization`, `X-Request-ID`, `X-Session-ID`, `X-Client-Version`.

**Black-box boundary:** The header's *name* is specified; its *contents and validation* are not.

**Missing technical definitions:**
1. What scheme the `Authorization` header uses (Bearer JWT, API key, mTLS client cert presented at a different layer, Basic auth against the LDAP/AD-backed Identity Service).
2. How and where that credential is obtained by the CLI in the first place (see BB-023 — no login flow is documented).
3. Whether the same credential is valid across the CLI's various commands (`/task`, `/approve`, etc.) or whether `/approve` requires a stronger form of authentication given its consequence.
4. Token refresh behavior for long-running interactive CLI sessions.

**Why this is a black box:** This is the literal front door of the entire system, and it is represented by a single header name in a "recommended" list with no further definition — an implementer has to invent an entire authentication scheme independently of this document.

---

### BB-025 — Read-Path Ownership Ambiguity and Missing Schemas

**Component:** CLI / Orchestrator / Control Plane

**Source:** `01_CLI.md` §/status, §Command contract; `03_ORCHESTRATOR.md` §Orchestrator interfaces.

**Current specification:** `01` states `/status` "reads only task state exposed by the control/data APIs." `03` separately defines `GET /tasks/{id}` and `GET /tasks/{id}/activity` as Orchestrator-owned endpoints. `01`'s Command Contract section gives full JSON request bodies for `/task` and `/approve` but none for `/artifacts` or `/trace`, both of which appear in the prototype acceptance test.

**Black-box boundary:** Three plausible owners (Control Plane, Data Plane, Orchestrator) are each independently suggested for serving the same read (task status), and two of the six documented CLI commands have no defined contract at all.

**Missing technical definitions:**
1. Which single service actually serves `/status` — the phrase "control/data APIs" in `01` doesn't resolve whether it means "the Control Plane's API and the Data Plane's API" (implying the CLI itself aggregates two calls) or is loosely referring to backend APIs in general, one of which happens to be the Orchestrator's own `GET /tasks/{id}`.
2. The request/response schema for `/artifacts` (does it call into the Data Plane's artifact metadata directly, or through the Orchestrator?).
3. The request/response schema for `/trace` (does it query Observability directly, or through the Orchestrator?).
4. Whether the CLI is allowed to call multiple backend services directly, or must always route everything through a single API surface (the Query Router) — `01`'s own "Important rule" section restricts what credentials the CLI holds but doesn't state whether it can address multiple service endpoints.

**Why this is a black box:** Three different documents each imply a different service is authoritative for the same read operation, and this is compounded by two of six documented commands simply lacking a contract — an implementer has to make an ownership decision the specification never made.

---

### BB-026 — Working Memory & Organizational Knowledge Have No Schema

**Component:** Data Plane

**Source:** `06_DATA_PLANE.md` §6 Memory types.

**Current specification:** Four memory types are distinguished conceptually: Working memory ("current agent execution"), Task memory ("facts/decisions for one task"), Organizational knowledge ("persistent approved knowledge base"), and Execution history ("immutable events"). Of these four, only Task memory receives a concrete JSON shape, and only via a different document (`09_SANDBOX_DETAILED_STATE.md`'s Shared State object). Execution history is likewise given a concrete shape via the event schema in `00`/`08`.

**Black-box boundary:** The document explicitly instructs "do not mix them into one generic memory object" — asserting these are meaningfully distinct systems — but only defines two of the four.

**Missing technical definitions:**
1. Where Working Memory physically lives — is it purely in-process/in-container state that dies with the agent (per `09`'s `agent_state.working_memory: []`), or a Data Plane-backed store that persists across agent restarts?
2. What schema Organizational Knowledge uses, and whether it is queried the same way as Task memory (Postgres-style structured queries) or the same way as the RAG vector store (semantic search) — see also BB-027.
3. How something graduates from Task memory into Organizational Knowledge (is there an approval or promotion step, given Organizational Knowledge is described as "approved").
4. Whether Working Memory is subject to the same ACL/classification rules as the other three memory types, given it may contain intermediate reasoning derived from confidential retrieved evidence.

**Why this is a black box:** The instruction to keep these as four separate concepts is a real architectural commitment, but only half of the four concepts are specified well enough to build against — the other half exist only as names in a bullet list.

---

### BB-027 — Organizational Knowledge vs. RAG Vector Store Relationship

**Component:** Data Plane / Local RAG

**Source:** `06_DATA_PLANE.md` §6 Memory types vs. `10_LOCAL_RAG.md` §Pipeline.

**Current specification:** `06` names "Organizational knowledge → persistent approved knowledge base" as one of four memory types. `10` independently describes an ingestion pipeline (Files → Ingestion → OCR → Metadata/ACL tagging → Chunking → Embedding → Vector DB) that also produces a persistent, ACL-tagged, organization-wide knowledge corpus.

**Black-box boundary:** Both descriptions are plausibly describing the same underlying store, but neither document references the other, and neither uses matching terminology.

**Missing technical definitions:**
1. Whether "Organizational knowledge" in `06` *is* the RAG vector store from `10`, or a separate structured knowledge base that RAG draws from in addition to.
2. If they are the same system, whether Organizational Knowledge access (mentioned in `06` without any retrieval semantics) uses the ACL-aware retrieval pipeline defined in `10`, or some other access pattern.
3. If they are different systems, how facts move between them, and which one deliverable-generation and audit/provenance features are meant to reference.

**Why this is a black box:** Two documents each assert ownership of "the organization's approved persistent knowledge" using non-overlapping vocabulary, and nothing reconciles them — an implementer following `06` alone and one following `10` alone would build two different, non-integrated stores.

---

### BB-028 — Provenance Record Storage/Query Mechanism

**Component:** Data Plane

**Source:** `06_DATA_PLANE.md` §7 Provenance.

**Current specification:** Six fields every piece of generated evidence "should point to" are listed: source document, source version, page/section, retrieval event, agent/model that used it, artifact where it appeared.

**Black-box boundary:** The fields a provenance record needs are named; the record itself — its schema, its storage location, and how it's queried to produce the provenance view shown in `08_OBSERVABILITY.md` — is not.

**Missing technical definitions:**
1. Whether provenance is a first-class table/collection of its own, or is reconstructed on demand by joining the task-state store, the event log, and artifact metadata.
2. How a provenance chain is queried given an `artifact_id` (per the example flow in `08` §Provenance view, which shows a multi-hop traversal from artifact → agent → model → retrieved documents → approval).
3. How provenance for a piece of evidence that touched multiple agents/models across a revised plan (BB-003) is represented — a single record or a chain.
4. Whether provenance data is retained under the same rules as the immutable audit-event log, or under the "approved artifacts" retention tier (see BB-029), given it can outlive the task that produced it.

**Why this is a black box:** `08`'s dashboard example presupposes a fully queryable provenance graph exists, but the only document that discusses provenance as data (`06`) describes it purely as a list of fields an evidence item "should point to," not as a queryable structure.

---

### BB-029 — Retention/Purge Mechanism and Interaction With Immutable Audit Trail

**Component:** Data Plane

**Source:** `06_DATA_PLANE.md` §8 Retention.

**Current specification:** Three retention tiers are named: temporary task data (purge after a "policy-defined window"), approved artifacts (organizational retention policy), audit events (immutable retention policy).

**Black-box boundary:** The tiers and their general retention posture are named; the mechanism that actually purges data, and what happens to references *into* purged data from data that must NOT be purged, is not addressed.

**Missing technical definitions:**
1. What process triggers a purge (a scheduled job, a task-completion event, a manual administrative action) and where the "policy-defined window" configuration itself lives.
2. How purging temporary task data (e.g., a `discovered_fact` in Shared State) is reconciled with a permanently-retained RELEASED artifact whose provenance record (BB-028) cites that same fact as one of its sources — does the provenance link break, get materialized into the artifact's own retained record before purge, or is temporary data quietly excluded from the purge window if any released artifact still cites it?
3. Whether the immutable audit event log (which the specification says must be retained permanently) itself contains enough embedded content (beyond `input_hash`/`output_hash`) to reconstruct purged temporary data, which would make "purge" only partially effective as a confidentiality control.
4. How retention interacts with the backup copies described in BB-030 — whether purged data is also removed from backups.

**Why this is a black box:** This is a genuine tension, not just a missing parameter: the specification simultaneously promises confidential data gets purged and promises that approved artifacts and their provenance are retained permanently and immutably, without ever stating which promise wins when a purged fact is cited by a retained artifact.

---

### BB-030 — Cross-Store Consistent Backup/Restore Procedure

**Component:** Data Plane

**Source:** `06_DATA_PLANE.md` §9 Backup.

**Current specification:** A list of systems requiring backup is given: Postgres, vector DB, object store, policy configuration, model manifests, audit events. The requirement that backups "must also remain inside the approved infrastructure" is stated for sovereignty reasons.

**Black-box boundary:** What must be backed up is listed; how a coherent, restorable backup is produced across three structurally different storage systems that reference each other (Postgres task records point at object-store artifact IDs and vector-DB document IDs) is not addressed.

**Missing technical definitions:**
1. Whether backups across the different stores are taken at a coordinated point in time (to avoid a restored Postgres row referencing an object-store artifact that wasn't backed up yet, or vice versa), and by what mechanism.
2. Backup frequency and retention of backup generations.
3. The restore procedure and its expected data-loss window (RPO) and downtime (RTO).
4. Whether policy configuration and model manifests (described as needing backup) are stored in Postgres alongside task state, or in separate configuration stores with their own backup path.

**Why this is a black box:** Naming the systems that need backing up is a reasonable level of abstraction on its own, but the cross-store referential integrity problem it creates (a hallmark of any system split across an RDBMS, a vector DB, and an object store) is a real technical decision point the specification doesn't acknowledge exists.

---

### BB-031 — Network-Identity Attribution Mechanism

**Component:** Network Layer

**Source:** `07_NETWORK_LAYER.md` §Network identities, §Zero-egress demonstration.

**Current specification:** A logging schema associates each connection with `task_id`, `agent_id`, `container_id`, `user_id`, `destination`, `port`, `policy_decision_id`, byte counts, and a timestamp. Enforcement is suggested via "nftables or an equivalent policy mechanism" operating at the host/container level.

**Black-box boundary:** The fields to log are fully specified; the mechanism that connects a raw network event (which, at the nftables/kernel level, knows about IP addresses, ports, and perhaps a cgroup/container ID) to task-level identifiers like `task_id`, `agent_id`, and `user_id` is not.

**Missing technical definitions:**
1. How a kernel-level or container-runtime-level network event is correlated with the higher-level `task_id`/`agent_id`/`user_id` — via container labeling read by a sidecar, via a lookup table maintained elsewhere, or via some other mechanism.
2. Whether this correlation happens synchronously (blocking the connection attempt until identity is resolved) or asynchronously (logged after the fact, which would weaken the "zero-egress demonstration" claim if a blocked connection can't yet be attributed at block time).
3. What component maintains the container-ID-to-task/agent-ID mapping, and how it's kept in sync with the Execution Plane's container lifecycle (a container recreated on crash-recovery, per BB-018, presumably gets a new container_id needing to be re-associated).
4. How the same attribution mechanism applies to the `policy_decision_id` field — implying a live link back to a Control Plane decision — when the network layer is elsewhere described as deliberately independent ("No application component should directly decide that an external route is allowed," `12` §9).

**Why this is a black box:** The zero-egress proof this component exists to provide (07 §Zero-egress demonstration) is only as strong as the attribution linking a blocked packet to a specific task — and that link is exactly the part left unspecified.

---

### BB-032 — Egress-Authorization Interface for Controlled Connectivity

**Component:** Network Layer / Control Plane

**Source:** `07_NETWORK_LAYER.md` §Mode B; `12_INTEGRATION_CONTRACTS.md` §9.

**Current specification:** Mode B ("Controlled connected deployment") names an "Egress Gateway" and an "Allow-list" that permitted traffic passes through, following the flow `Agents → Control Plane → Egress Gateway → Allow-list → Approved endpoint`. `12` §9 states only that "no application component should directly decide" egress is allowed, treating network permission as "a separate enforcement layer."

**Black-box boundary:** Mode B explicitly anticipates a workflow where a component requests, and is granted, a specific external connection — but no contract exists anywhere for how that request/grant actually happens.

**Missing technical definitions:**
1. What interface a component (Orchestrator, or an agent via the Tool Gateway) uses to request an external route be added to, or matched against, the allow-list.
2. Whether allow-list entries are static configuration set up ahead of time by an administrator, or dynamically requestable per-task through the Control Plane.
3. How a Mode B external connection request interacts with the capability-token mechanism (BB-020) used for internal tool calls — is network access itself modeled as just another capability grant?
4. How the Egress Gateway differs, mechanically, from the "Tool Gateway" already described in `04`/`12` §5–6 — whether it's the same component wearing a different name for external destinations, or a genuinely separate piece of infrastructure.

**Why this is a black box:** Mode B is presented as a first-class supported deployment mode, not a footnote, yet it is the one part of the network architecture with zero worked example, unlike every other integration point in `12`.

---

### BB-033 — Observability Summarization Step

**Component:** Observability

**Source:** `08_OBSERVABILITY.md` §Agent trace.

**Current specification:** "Do not store hidden chain-of-thought as an observability requirement. Record concise action/decision summaries, tool inputs/outputs where permitted, provenance, and hashes."

**Black-box boundary:** The instruction establishes *what* should be recorded (a summary, not raw chain-of-thought) but not *how* that summary is produced.

**Missing technical definitions:**
1. Whether producing a "concise action/decision summary" requires its own model inference call, separate from whatever call produced the underlying action.
2. If it is a model call, whether it goes through the Model Router and requires its own capability grant like any other model use described in `00` §2 — or is treated as exempt because it's "just" observability.
3. What happens to the summary if the underlying content is itself classified/confidential — does the summarization call have the same data-classification constraints as the original action (per `02`'s model-classification-compatibility rule, BB-007)?
4. Who/what decides "where permitted" for recording raw tool inputs/outputs — a policy check, a static per-tool flag, or something else.

**Why this is a black box:** This is a subtle but real integration gap: an entire category of model usage (summarization for audit purposes) is introduced without being folded into the capability-grant/policy-check pipeline that the specification insists governs "every action" an agent or the system takes on confidential data.

---

### BB-034 — Evaluation/Verification Scoring Methodology

**Component:** Observability / Local RAG

**Source:** `08_OBSERVABILITY.md` §Evaluation; `10_LOCAL_RAG.md` §RAG quality checks.

**Current specification:** `08` lists evaluation dimensions (task completion, source grounding, policy compliance, artifact validity, human approval rate, regression against previous model) to be run against a "local test set" of golden tasks. `10` separately lists RAG-specific quality metrics (retrieval precision, source coverage, citation correctness, stale document usage, ACL violations).

**Black-box boundary:** Both documents name what should be measured; neither states how any individual metric is actually computed.

**Missing technical definitions:**
1. Whether "task completion" and "source grounding" are scored by a human rater, a rule-based checker (e.g., string-matching expected outputs), or a model-as-judge approach.
2. How "citation correctness" is verified — does it require re-fetching the cited source and comparing text, or trusting the retrieval-time provenance record?
3. What the "golden task" format is (an expected output to diff against, a rubric, a set of assertions) — none is shown.
4. How "regression against previous model" is computed when the underlying models are swapped via the Model Router (does this require running the full golden set against both old and new models on every model change?).

**Why this is a black box:** Naming evaluation dimensions is a reasonable starting point, but none of the six-plus metrics named across both documents has an associated measurement procedure, which means "evaluation" as a capability doesn't yet exist as a specified system — only as a list of things it should someday report on.

---

### BB-035 — Hash-Chain Ordering Under Concurrent Multi-Service Event Emission

**Component:** Observability

**Source:** `08_OBSERVABILITY.md` §Immutable events.

**Current specification:** A precise formula is given: `event_n.hash = SHA256(event_n.payload + event_(n-1).hash)`. The Observability plane is explicitly described (00 §Observability, cross-cutting diagram) as receiving events from every major block — CLI, Routers, Orchestrator, Control Plane, Execution/Data Plane, and Network — simultaneously.

**Black-box boundary:** The hash formula itself is fully specified and requires no clarification. What is unspecified is how a strictly linear chain (each hash depending on exactly one predecessor) is maintained when the specified event sources are five-plus independent, concurrently-operating services.

**Missing technical definitions:**
1. Whether there is a single centralized writer/sequencer that all services send events to (which would resolve ordering but isn't described as existing anywhere — each service is elsewhere described as emitting its own events directly), or whether each service maintains its own independent hash chain.
2. If chains are per-service, how a "single audit trail" (implied by the tamper-evidence goal and by the unified `event_id` namespace in `00`'s Universal event format) is reconstructed across them for a single task that touches every service.
3. What happens when two events with the same nominal predecessor are emitted at effectively the same time by two different services — which one becomes `event_(n-1)` for the other, and how is that decided without a shared ordering authority.
4. How this reconciles with the optimistic-concurrency version numbers used elsewhere (BB-011) — whether event sequence numbers and state version numbers are the same counter or two independent ones.

**Why this is a black box:** A tamper-evident hash chain is only tamper-evident if its ordering is unambiguous and enforced; the specification provides the cryptographic formula but not the concurrency-control mechanism the formula depends on, in a system explicitly designed to have many concurrent event sources.

---

### BB-036 — ACL-Tagging Mechanism at Document Ingestion

**Component:** Local RAG

**Source:** `10_LOCAL_RAG.md` §Pipeline.

**Current specification:** The ingestion pipeline names "Metadata + ACL tagging" as a discrete stage between OCR/structure extraction and chunking. The resulting per-document metadata schema includes `department`, `sector`, `classification`, and `acl` fields, and ADR-004 (`14_ARCHITECTURE_DECISIONS.md`) explicitly justifies why ACL enforcement must happen *before* retrieval results reach the model.

**Black-box boundary:** The pipeline names the tagging stage and the resulting schema; it never states who or what actually assigns those values to a given incoming document.

**Missing technical definitions:**
1. Whether ACL/classification tags are assigned automatically (inherited from a source folder's existing permissions, inferred from document content/metadata, inherited from an existing document-management system's ACLs) or manually by a human during ingestion.
2. What happens to a document for which no reliable classification/ACL source exists — is it rejected, quarantined, or ingested with a default (and if so, which default: most-restrictive or least-restrictive)?
3. Who is authorized to change a document's classification/ACL after ingestion, and how already-created chunks/embeddings are updated to reflect a change.
4. How the tagging step is itself audited (given every other privileged action in the system produces an audit event, per `05` §Audit model) — no `DOCUMENT_TAGGED` or similar event type appears in the event-type catalog.

**Why this is a black box:** ADR-004 stakes a specific, deliberate security claim ("post-filtering is weaker... unauthorized records may already have crossed a trust boundary") on ACL tags being correct at ingestion time — but the mechanism that produces those tags in the first place, which is the actual source of ground truth the entire ACL-aware retrieval guarantee depends on, is completely unaddressed. A wrong or missing tag at this single, unspecified step silently defeats the security property the rest of the RAG pipeline is built to preserve.

---

### BB-037 — Evidence Package Drops Classification/ACL Fields Needed Downstream

**Component:** Local RAG / Deliverables

**Source:** `10_LOCAL_RAG.md` §Evidence package vs. `11_DELIVERABLES.md` §Verification.

**Current specification:** Chunk metadata (`10` §Chunking) carries `classification` and `acl`. The evidence package returned to the agent after retrieval (`10` §Evidence package) has a narrower schema: `document_id`, `page`, `text`, `version` — no classification or ACL field. Separately, `11`'s deliverable verification step is required to check "classification markings correct" and "no unauthorized source" against the sources a deliverable actually used.

**Black-box boundary:** One document defines the evidence handed to agents/deliverable-builders without the very field another document requires the verification step to check.

**Missing technical definitions:**
1. Whether classification/ACL data is silently dropped from the evidence package (in which case `11`'s verification step has nothing to check against without a separate lookup) or is meant to be re-fetched by document/version ID at verification time.
2. Whether the evidence package schema shown is a simplified example or the literal complete contract — nothing in either document flags it as illustrative.
3. If a re-fetch is intended, what interface performs it and under what authorization (since it would itself be a data-classification-sensitive read, subject to the same ACL rules already applied once during retrieval).

**Why this is a black box:** This is a concrete, traceable schema mismatch between two pipeline stages that are supposed to compose — an engineer building the evidence package literally as specified in `10` would hand the `11` verification step an object it cannot use to do the check `11` requires of it.

---

### BB-038 — Verification Algorithm and Business-Rule Configuration

**Component:** Deliverables

**Source:** `11_DELIVERABLES.md` §Verification, §Business rules.

**Current specification:** Four verification categories are named (file validity, content, policy, business rules), each with a short bullet list of what's checked. One example business rule is given ("repair cost must match approved source").

**Black-box boundary:** What gets checked is enumerated at a conceptual level; how any individual check is actually performed, and where business rules themselves are authored/stored, is not.

**Missing technical definitions:**
1. Whether content checks like "numbers consistent" and "citations attached" are performed by deterministic parsing/validation code, or by a model reviewing the generated document (which would itself need a capability grant and model routing, per `00`/`02`).
2. Where business rules (like the repair-cost example) are defined — a per-organization configuration file, a rules-engine DSL, hardcoded per document template — and who is authorized to add or change them.
3. How a business rule like "repair cost must match approved source" is technically evaluated against a generated DOCX/PPTX/XLSX artifact (does it require re-parsing the generated file, or is it checked against the pre-generation evidence/decisions before rendering?).
4. What happens when verification fails — is the artifact rejected outright, sent back to the Orchestrator for a plan revision (BB-003), or held for human review regardless?

**Why this is a black box:** This step is the last automated gate before a document reaches a human approver in a workflow whose entire value proposition (11 §Why this is strategically important) is trustworthy, verified output — yet the actual verification logic is the least specified part of that pipeline.

---

### BB-039 — Immutability Enforcement for RELEASED Artifacts

**Component:** Deliverables

**Source:** `11_DELIVERABLES.md` §Release.

**Current specification:** "Once approved: artifact version immutable, approval identity recorded, hash recorded, provenance recorded."

**Black-box boundary:** Immutability is asserted as a property of a RELEASED artifact; the technical control that makes it actually immutable (as opposed to merely policy that says it shouldn't be changed) is not named.

**Missing technical definitions:**
1. Whether immutability is enforced by storage-layer means (write-once object storage, a locked bucket, filesystem permissions) or purely by access-control policy in the Control Plane (which would be circumventable by anyone with sufficient privilege at the storage layer).
2. Whether the recorded hash (per the artifact metadata schema in `09`/`11`) is periodically re-verified against the stored object to detect tampering, or only computed once at release time.
3. How this interacts with the QUARANTINE ARTIFACT emergency control (BB-021), which implies a RELEASED artifact's state can, in fact, change after release.
4. Whether "immutable" applies to the artifact's binary content only, or also to its metadata (approval identity, provenance) — and if the latter is ever legitimately correctable (e.g., fixing a mis-recorded `approved_by` field).

**Why this is a black box:** "Immutable" is a strong claim used to support the system's auditability story, but the specification never distinguishes between "immutable by policy" and "immutable by technical control," which are very different guarantees.

---

### BB-040 — Tool Gateway → Data Plane Contract Left Unspecified

**Component:** Integration Contracts

**Source:** `12_INTEGRATION_CONTRACTS.md` §6.

**Current specification:** "The tool gateway should enforce: authorization context, ACL filters, scope, read/write operation, audit event." No JSON example is given.

**Black-box boundary:** Every other numbered section in this document (§1–§5, §7) gives a concrete request and/or response JSON example. §6 is the sole exception, despite covering what is arguably the single most security-critical hop in the entire request path — the point where a tool call actually touches confidential organizational data.

**Missing technical definitions:**
1. The actual request schema the Tool Gateway sends to the Data Plane (what does "ACL filters" look like as a wire format — a list of allowed ACL tags? a compiled query predicate?).
2. The response schema, including how a partial-authorization result (some requested data allowed, some denied) is represented, if that's possible.
3. Whether this call carries the capability token from BB-020 directly, or a Data-Plane-specific credential derived from it.
4. Whether this is a synchronous call per tool invocation or something else, given the "audit event" is listed as one of the things enforced at this hop — is the audit event emitted by the Tool Gateway, the Data Plane, or both?

**Why this is a black box:** The internal inconsistency is itself evidence of a gap: nothing about this interaction is inherently harder to specify than the other six, which suggests it was simply not thought through to the same level, at exactly the point where under-specification is most consequential.

---

### BB-041 — GPU Resource Representation Mismatch; No Contention Model

**Component:** Integration Contracts / Model Router

**Source:** `12_INTEGRATION_CONTRACTS.md` §4; `02_QUERY_MODEL_ROUTER.md` §Model capability manifest.

**Current specification:** The sandbox-creation request in `12` §4 represents GPU need as a boolean: `"gpu": true`. The model manifest in `02` represents GPU need as a specific quantity: `"min_vram_gb": 24`. Nothing else in the specification discusses how GPU resources are allocated among concurrent tasks/containers.

**Black-box boundary:** A boolean cannot carry a specific VRAM requirement, so the two schemas that are supposed to compose (a container needs to be sized to run a model that has a specific VRAM requirement) do not actually compose as written. Beyond the schema mismatch, GPU scheduling itself — which containers get which physical/virtual GPU when several tasks run concurrently — is unaddressed anywhere.

**Missing technical definitions:**
1. How the sandbox-creation request is meant to communicate an actual VRAM/GPU-count requirement, given the shown schema only supports true/false.
2. Whether GPU allocation is exclusive per container, time-shared, or MIG/vGPU-partitioned, and who decides.
3. What happens when a task requiring a GPU-bound model is submitted while all GPUs are already allocated to other running tasks — is it queued, rejected, or does the Model Router's routing score (BB-006) factor in current GPU availability via its `resource_penalty` term?
4. Whether GPU contention interacts with the Model Router's model-unavailability fallback chain (BB-008) — i.e., is "the model is technically healthy but no GPU is free" treated the same as "the model server is down"?

**Why this is a black box:** For a system whose entire value proposition depends on running local, GPU-hosted models, the resource that is almost certainly the tightest bottleneck in any real deployment has no scheduling model at all, and the one schema field meant to carry GPU requirements into a container-creation request can't actually carry the information the model-selection layer says it needs.

---

### BB-042 — Deployment/Process Topology and Inter-Component Trust Boundary

**Component:** Cross-cutting

**Source:** `00_MASTER_ARCHITECTURE.md` §2, §9; `13_MVP_IMPLEMENTATION_PLAN.md` §Suggested repository; `14_ARCHITECTURE_DECISIONS.md` ADR-007.

**Current specification:** `00` §2 states the "most important rule" of the entire system: agents "request capabilities; the control plane decides." §9 lists a defense-in-depth chain including "Sandbox isolation" as a layer distinct from "Capability grant." `13` proposes a single repository with folders per component (`cli/`, `router/`, `orchestrator/`, `control/`, `execution/`, `data/`, `network/`, `observability/`, `models/`). ADR-007 explicitly recommends "one-machine orchestration plus containers and Postgres" for the MVP, reasoning that "the first technical risk to prove is the security/workflow model, not horizontal scaling."

**Black-box boundary:** Every security property claimed for the Control Plane (that it is a real choke point agents cannot bypass) implicitly assumes it is a genuine trust boundary — a process or service an agent cannot simply call into or read the memory of. Nothing in the specification states whether this is true.

**Missing technical definitions:**
1. Whether Control Plane, Orchestrator, Execution Plane (outside of the agent containers themselves), and Data Plane run as separate OS processes/services with their own network identity and independent authentication, or as importable modules within a single Python process, as the single-repository, single-machine framing in `13`/ADR-007 could equally support.
2. If they are separate processes on the same machine, what mechanism prevents a process with Orchestrator-level access from also having Control-Plane-level access (shared host, shared filesystem, potentially shared database credentials).
3. Whether "Sandbox isolation" (container boundary around agents) is the *only* real trust boundary in the MVP, with everything else being cooperating code in one trust domain — which would mean the elaborate policy/capability/audit model described in `05` is enforced by convention among mutually-trusting modules rather than by an actual security boundary.
4. What changes, if anything, about the security guarantees between the MVP topology and a hypothetical later "horizontal scaling" topology that ADR-007 defers.

**Why this is a black box:** This is the foundational assumption the entire governance story (00 §2, §9) rests on, and it is also the one thing the MVP-scoping documents (13, ADR-007) most directly speak to — in a direction that suggests the trust boundary may not exist for the prototype. Two engineers could build a materially different, differently-secure system from the same instructions: one with Control Plane as a genuinely separate, independently-authenticated service, and one with "Control Plane" as a Python module the Orchestrator imports and calls directly.

---

### BB-043 — "Verifier" Has No Owning Component

**Component:** Cross-cutting / Deliverables

**Source:** `00_MASTER_ARCHITECTURE.md` §7 step 12; `11_DELIVERABLES.md` (entire document).

**Current specification:** The golden path names "Verifier checks: citations, file integrity, policy compliance" as step 12 of 17. `11`'s pipeline separately shows "Automated verification" as a stage between artifact generation and human approval, with categories (file validity, content, policy, business rules) matching roughly what the golden path attributes to "Verifier."

**Black-box boundary:** Every other named actor in the golden path — CLI, Query Router, Model Router, Orchestrator, Control Plane, Execution Plane, agents, the deliverable builder, the approver — corresponds to a component with its own document, interfaces, and (for most) a place in the "Core components" table in `00` §3. "Verifier" does not appear in that table at all, and has no document of its own.

**Missing technical definitions:**
1. Whether Verifier is a distinct service, a function inside the Deliverables/Artifact pipeline, or a mode of the Orchestrator.
2. What interface a Verifier would expose or consume — none of the ten integration points in `12_INTEGRATION_CONTRACTS.md` mention a Verifier or verification step.
3. Whether policy compliance checking here duplicates or differs from the Control Plane's own policy engine (05) — the golden path lists "policy compliance" as something Verifier checks, which could mean Verifier re-implements policy logic, or calls back into the Control Plane's existing Policy Engine.
4. Where citation/file-integrity checks (also unspecified per BB-038) actually execute — inside an agent's sandbox, as a separate privileged process, or elsewhere.

**Why this is a black box:** Naming something consistently across two documents (the golden path and the Deliverables pipeline) without ever giving it the same architectural treatment as every other named component is itself a specification gap distinct from, and larger than, any single missing algorithm — it's a missing component.

---

### BB-044 — Shared-State Write Access Enforced Only by Convention

**Component:** Sandbox / Data Plane

**Source:** `09_SANDBOX_DETAILED_STATE.md` §Shared State.

**Current specification:** "Only orchestrator-approved mutations should update shared state."

**Black-box boundary:** This is phrased as a design intention ("should"), not a described technical control. No file states what actually prevents an agent — which, per `04`, runs arbitrary submitted code inside its sandbox — from opening a direct connection to wherever Shared State lives (Postgres, per `06`) and writing to it without going through the Orchestrator at all.

**Missing technical definitions:**
1. Whether agent containers have network-level access to the Data Plane's Postgres instance at all, or whether default-deny network policy (04 §Isolation controls) already precludes this by construction — this is asserted for "Internet" access but never explicitly confirmed for internal Data Plane access.
2. If agent containers can reach Postgres, what prevents them from holding or obtaining credentials capable of writing to the shared-state table.
3. Whether "orchestrator-approved" mutation is enforced by the Orchestrator being the only holder of write credentials, or by some other mechanism (e.g., a database-level policy, a mandatory Tool-Gateway hop for all writes).
4. How this reconciles with the Tool-Gateway-mediated write path implied by `12` §6/§7 (Tool Gateway → Data Plane, Data Plane → Orchestrator) — if all writes are supposed to go through that path, no document states that this is the *only* path available to a running agent container.

**Why this is a black box:** "Should" is a design principle, not an enforcement mechanism — and given agents in this architecture run submitted code, the gap between "agents shouldn't write shared state directly" and "agents technically cannot write shared state directly" is exactly where a real vulnerability would live if left unresolved.

---

### BB-045 — Template-Selection Mechanism

**Component:** Deliverables

**Source:** `11_DELIVERABLES.md` §Pipeline.

**Current specification:** "Template selection" is named as the pipeline stage between "Evidence + agent result" and "Artifact generation."

**Black-box boundary:** The stage is named; the selection logic is not described at all.

**Missing technical definitions:**
1. Whether template choice is driven by task type/domain (from the Query Router's classification, per `02`), by explicit user selection, or by the artifact's declared output type.
2. Where the set of available templates is stored/versioned, and how the Template model fields (organization style, approval fields, classification markings, etc., per `11` §Template model) get populated per selection.

**Why this is a black box:** This is a narrower gap than most in this report — a single pipeline stage with an obvious but unstated selection rule — which is why it is scored Low rather than Medium or High; it does not block a workable implementation the way the higher-severity findings do, but it is still a genuine decision point with no stated resolution.

---

### BB-046 — Zero-Egress Dashboard Counter Computation

**Component:** Network Layer

**Source:** `07_NETWORK_LAYER.md` §Zero-egress demonstration.

**Current specification:** Example counters are shown for a running task: `attempts counter = 0`, `blocked attempts = N`, `allowed external bytes = 0`.

**Black-box boundary:** The counters and their intended demonstrative purpose are given; how they are computed, aggregated, or refreshed is not.

**Missing technical definitions:**
1. Whether these counters are computed live from the network-identity log described in BB-031, or maintained as a separate running total.
2. Whether the counters are per-task, per-agent, or system-wide, and how they reset (per task start, never, on a rolling window).

**Why this is a black box:** Like BB-045, this is a narrow, low-consequence gap given the underlying data it would be computed from (BB-031) is itself the more significant unresolved piece — this finding mainly flags that the aggregation layer on top of that data is also unaddressed.

---

### BB-047 — Approval-Decision Propagation Interface Missing

**Component:** Control Plane

**Source:** Absent from `12_INTEGRATION_CONTRACTS.md` (a ten-section document enumerating every other major hand-off, with no "Approval" section); `05_CONTROL_PLANE.md` §Human approval; `14_ARCHITECTURE_DECISIONS.md` ADR-005.

**Current specification:** `05` gives an approver-decision object schema (`artifact_id`, `approver_id`, `decision`, `comment`, `timestamp`, `previous_hash`) and names four approval-gate states (DRAFT, REVIEW_REQUIRED, APPROVED, RELEASED). ADR-005 explicitly justifies treating approval as "a state transition" that "generates a signed/auditable event," specifically to create "a clear chain of accountability."

**Black-box boundary:** `12_INTEGRATION_CONTRACTS.md` is the document whose stated purpose is to enumerate every service-to-service contract in the system (§1 CLI→Query Router through §9 Network, plus §8 Observability and §10 the general integration rule). It contains no equivalent section for what happens after a human approval decision is made — there is no contract showing how that decision reaches the Data Plane (to update the artifact record) or the Orchestrator (to advance the task's state machine).

**Missing technical definitions:**
1. What component receives the approval decision from the CLI's `/approve` command after Control Plane validates the approver's authorization — is it written directly to the Data Plane, does it trigger an Orchestrator callback, or both?
2. What payload actually flows from Control Plane onward — the full approver-decision object, or a derived event.
3. How the artifact's/task's state machine (whichever of the several described in BB elsewhere — see Contradiction C-001) is actually advanced as a consequence of this decision, given the object schema in `05` describes the decision but not its effect.
4. What happens on REJECT specifically — `00`'s task state machine shows `WAITING_FOR_APPROVAL → REJECTED → REVISION`, implying the Orchestrator must be notified to begin a revision (BB-003), but no contract shows that notification.

**Why this is a black box:** Approval is repeatedly emphasized as one of the architecture's central trust mechanisms (it is the subject of its own ADR, its own state-machine states in three separate documents, and the CLI's only command besides `/task` given a full JSON schema) — yet the actual wiring that makes an approval decision *do* anything is the one major hand-off omitted from the document built specifically to catalog hand-offs.

---

## Specification Contradictions / Conflicts

| ID | Documents | Conflict | Architectural Impact |
|----|-----------|----------|------------------------|
| C-001 | `00_MASTER_ARCHITECTURE.md` §8 (task state machine); `04_EXECUTION_PLANE_SANDBOX.md` §Artifact boundary; `05_CONTROL_PLANE.md` §Human approval; `11_DELIVERABLES.md` §Pipeline, §Approval gate | Four different state-machine vocabularies are used for what appears to be the same or overlapping concept (task lifecycle vs. artifact lifecycle vs. approval-gate lifecycle): `CREATED→PLANNING→WAITING_FOR_POLICY→RUNNING→WAITING_FOR_APPROVAL→REJECTED→REVISION→VERIFIED→APPROVED→RELEASED` (00); `TEMP→CANDIDATE→VERIFIED→APPROVED→RELEASED` (04); `DRAFT→REVIEW_REQUIRED→APPROVED→RELEASED` (05); `Draft→Automated verification→Human reviewer→Approve/Reject→Release` (11). They share terminal states (APPROVED, RELEASED) and near-synonyms (DRAFT/TEMP, REVIEW_REQUIRED/CANDIDATE) but are never declared to be the same state machine, different state machines for different objects (task vs. artifact), or mapped to one another. | An implementer cannot determine whether a task and its artifact share one status field or two, nor how a transition in one is supposed to trigger a transition in the other. This directly affects BB-047 (approval propagation) and BB-021 (QUARANTINE ARTIFACT's target state). |
| C-002 | `04_EXECUTION_PLANE_SANDBOX.md` §Tool gateway; `05_CONTROL_PLANE.md` §Main services; `12_INTEGRATION_CONTRACTS.md` §3, §5, §6 | `04` draws every tool call as passing through the Control Plane inline (`Agent → gateway → Control Plane → Tool`). `12` shows a capability token issued once (§3) and then used directly against the Tool Gateway (§5) with the Data Plane as the next hop (§6) — no further Control Plane call appears. `05`'s own list of Control Plane services never mentions a Tool Gateway. | Two incompatible authorization topologies (re-check-every-call vs. check-once-then-trust-the-token) are both present in the specification, and the component that would implement either (Tool Gateway) isn't claimed by any documented service owner. See BB-015. |
| C-003 | `02_QUERY_MODEL_ROUTER.md` §Interfaces (`POST /v1/route/model`); `12_INTEGRATION_CONTRACTS.md` §2 (Query Router → Model Router) | `02` specifies this operation's response as a single model object: `{model_id, endpoint, fallback_model_id, reason}`. `12` specifies what reads as the same operation, using the same running example (a task needing vision + reasoning + embedding), returning a completely different shape: `{selected_models: {vision: "...", reasoning: "...", embedding: "..."}}`. | The two documents that should jointly define the Model Router's external interface describe two different, non-composable response schemas for it. See BB-009. |
| C-004 | `13_MVP_IMPLEMENTATION_PLAN.md` §Phase 2, §Phase 3; `00_MASTER_ARCHITECTURE.md` §2 | `00` states the system's "most important rule": all agent actions "become policy-checked operations," with no stated exception. `13`'s Phase 2 deliverable is explicitly "`/task → plan → tool → observe → revise`" — i.e., a working tool-dispatch loop — while Phase 3, delivered *after* Phase 2, is what builds "Identity, Policy engine, Capability grants, Audit events, Approval gate." | The phased build plan has the system demonstrably exercising tool calls before the component that is supposed to gate every tool call exists, directly contradicting the stated absolute invariant. Either the invariant has an unstated MVP exception, or the phase plan's own deliverables are sequenced incorrectly relative to the architecture's stated non-negotiable rule. |
| C-005 | `01_CLI.md` §/status; `03_ORCHESTRATOR.md` §Orchestrator interfaces; `05_CONTROL_PLANE.md` §Main services | `01` attributes `/status` data to "the control/data APIs." `03` separately and specifically defines `GET /tasks/{id}` and `GET /tasks/{id}/activity` as Orchestrator-owned endpoints for what is functionally the same status information. `05`'s inventory of Control Plane services includes nothing resembling task-status serving. | Three documents imply three different authoritative owners (Control Plane, Data Plane, Orchestrator) for the same read path, with no stated aggregation or delegation between them. See BB-025. |

---

## Missing Boundaries Between Components

The table below walks the major interactions actually present in Citadel's architecture (as discovered from the files, not assumed from the audit brief's generic example list) and states whether a request/response contract exists, is partial, or is absent.

| Interaction | Contract status | Notes |
|---|---|---|
| CLI → Query Router | Partial | Endpoint and payload defined (`12` §1); authentication mechanism undefined (BB-024). |
| Query Router → Model Router | Present, but contradicted elsewhere | Defined in `12` §2; conflicts with `02`'s own interface definition for the same step (C-003). |
| **Query/Model Router → Orchestrator** | **Absent** | No document shows this hand-off at all (BB-004) — the single largest gap in the request path. |
| Orchestrator → Control Plane | Present | Fully defined with request/response JSON (`12` §3), including the capability-token issuance. |
| Orchestrator → Execution Plane (sandbox creation) | Partial | Request defined (`12` §4); no response, teardown, or health-check call shown; GPU field is under-specified (BB-041); ownership of who calls this is ambiguous (BB-013). |
| Agent → Tool Gateway | Partial | Request defined (`12` §5); no response schema shown; Tool Gateway's own ownership and authorization model is contradictory (C-002, BB-015). |
| Tool Gateway → Data Plane | Minimal | Only a prose list of enforcement responsibilities, no schema at all (BB-040) — a stark contrast with every sibling section. |
| Data Plane → Orchestrator (state commit) | Present | Fully defined, including the conflict-response case (`12` §7) — though what "merge" means on conflict is still undefined (BB-011). |
| Components → Observability | Present, generally | A universal event schema is given (`00` §6); ordering under concurrency is unaddressed (BB-035). |
| Components → Network Layer | Present, by design abstraction | `12` §9 deliberately treats this as a non-interactive enforcement layer — but Mode B's controlled-connectivity case needs a request/grant interface that doesn't exist (BB-032). |
| **Control Plane (Approval Service) → Data Plane / Orchestrator** | **Absent** | No contract exists anywhere for how a validated approval decision actually changes artifact/task state (BB-047) — omitted from the Integration Contracts document entirely. |
| Sandbox / Agent → Shared State (write path) | Ambiguous | Presumed to route through the Tool Gateway, but no document states this is the *only* available path, nor what technically prevents a direct write (BB-044). |
| CLI → read paths (`/status`, `/artifacts`, `/trace`) | Ambiguous / partial | Three different implied owners for `/status` (C-005); no schema at all for `/artifacts` or `/trace` (BB-025). |

---

## Black-Box Distribution

| Component | Critical | High | Medium | Low | Total |
|---|---:|---:|---:|---:|---:|
| Control Plane | 5 | 2 | 0 | 0 | 7 |
| Data Plane | 1 | 2 | 3 | 0 | 6 |
| Execution Plane / Sandbox | 1 | 3 | 1 | 0 | 5 |
| Orchestrator | 1 | 2 | 2 | 0 | 5 |
| Cross-Cutting / Architecture-Wide | 3 | 1 | 0 | 0 | 4 |
| Model Router | 0 | 3 | 1 | 0 | 4 |
| Network Layer | 0 | 1 | 1 | 1 | 3 |
| Observability | 0 | 1 | 2 | 0 | 3 |
| Deliverables | 0 | 1 | 1 | 1 | 3 |
| Local RAG | 1 | 0 | 1 | 0 | 2 |
| Integration Contracts | 0 | 2 | 0 | 0 | 2 |
| CLI | 0 | 1 | 1 | 0 | 2 |
| Query Router | 0 | 1 | 0 | 0 | 1 |
| **Total** | **12** | **20** | **13** | **2** | **47** |

### Most Black-Box-Heavy Components

1. **Control Plane (7 findings, 5 Critical)** — the component the entire security model is named after has the least-specified internals: its policy-decision algorithm, its capability-token mechanics, its emergency-control propagation, its secret-delivery mechanism, and the approval-decision hand-off out of it are all open. It is also implicated in two of the five direct contradictions (C-002, C-005).
2. **Data Plane (6 findings)** — half its named subsystems (Working Memory, Organizational Knowledge, the provenance graph) have no schema at all, and its retention model directly conflicts with the immutability guarantees made about released artifacts (BB-029).
3. **Execution Plane / Sandbox and Orchestrator (5 findings each)** — between them they hold the specification's sharpest internal contradiction (BB-016, code execution requiring container creation from a container denied Docker access) and its most consequential missing algorithm (BB-002, how a plan is actually generated).
4. **Cross-Cutting / Architecture-Wide (4 findings, 3 Critical)** — fewer in count but arguably the highest-leverage findings, since BB-001 (no agent design), BB-004 (no router-to-orchestrator hand-off), and BB-042 (no stated trust-boundary topology) each affect nearly every other finding in this report.

### Most Significant Specification Gaps

- **BB-001 — No agent execution model exists.** Every component in the specification is defined in terms of what it does *to* or *around* an agent (sandboxing it, granting it capabilities, checkpointing its state, logging its actions). The agent's own decision-making process — the actual "agentic" part of an "agentic AI workbench" — has no document, no schema, and no algorithm anywhere in the 15 files.
- **BB-004 / C-003 — The Query/Model Router never hands off to the Orchestrator, and the two documents that describe model selection disagree about what it even returns.** This is the seam between "figuring out what to do" and "doing it," and it's both missing and internally contradictory.
- **BB-015 / C-002 — The Tool Gateway's authorization model is asserted two incompatible ways, and no document claims to own it.** Since every agent action that touches data, tools, or models is supposed to flow through this exact mechanism, this is the single point where the "agents do not get direct authority" rule is least concretely enforced.
- **BB-016 — Sandboxed code execution appears to require violating the sandbox's own isolation rule.** This is not a missing detail but an apparent internal inconsistency within one document, at a point directly relevant to the system's core security claim.
- **BB-042 — No document states whether the Control Plane is a real trust boundary or a same-process module.** Every governance claim in `00`, `05`, and `14`'s ADR-001 assumes the former; the MVP-scoping guidance in `13` and ADR-007 is at least equally consistent with the latter.
- **BB-047 / C-001 — Approval, despite being the subject of its own ADR and three different state-machine descriptions, has no defined mechanism for actually taking effect**, and the state machines it's supposed to drive don't agree with each other on vocabulary in the first place.
- **BB-036 — ACL tagging at ingestion, the single fact the entire RAG security guarantee (ADR-004) depends on being correct, is not defined at all.**

