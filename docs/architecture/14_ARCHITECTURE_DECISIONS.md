# Architecture Decisions

## ADR-001 — Why Control Plane is between agents and data/tools

### Decision
Agents never receive unrestricted access to data, tools, or network.

### Reason
The core differentiator is governance, not simply local inference. Central mediation makes policy enforcement consistent across different agents and models.

---

## ADR-002 — Why models are resources

### Decision
Use capability manifests and a model router.

### Reason
The workbench must survive model changes without redesigning the orchestration layer.

---

## ADR-003 — Why state is external to the sandbox

### Decision
Persist task state, memory, artifacts and events outside disposable agent containers.

### Reason
Containers can fail or be recreated. Business state must survive failures.

---

## ADR-004 — Why RAG is ACL-aware before retrieval results reach the model

### Decision
Authorization filters apply during retrieval.

### Reason
Post-filtering is weaker because unauthorized records may already have crossed a trust boundary.

---

## ADR-005 — Why approval is a state transition

### Decision
Approval changes artifact/task state and generates a signed/auditable event.

### Reason
It creates a clear chain of accountability between AI-generated work and organizational acceptance.

---

## ADR-006 — Why network monitoring and enforcement are separate

### Decision
Use both:
- enforcement boundary
- independent telemetry

### Reason
A firewall can block an action; telemetry provides evidence that it attempted and was blocked. The combination supports "provable sovereignty."

---

## ADR-007 — Why the MVP should avoid premature distributed complexity

### Decision
Start with one-machine orchestration plus containers and Postgres.

### Reason
The first technical risk to prove is the security/workflow model, not horizontal scaling. Scale-out can be added after the control contracts are stable.
