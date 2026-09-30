# Block 07 — Network Layer / Egress Enforcement

## Role

This block makes the sovereignty claim measurable.

The security objective is:

> **No external data egress by default, and every permitted connection is attributable and auditable.**

## Two deployment modes

### Mode A — Truly air-gapped

```text
Workbench ─────X──── Internet
             physical isolation
```

Internet infrastructure is absent.

### Mode B — Controlled connected deployment

```text
Agents
  ↓
Control Plane
  ↓
Egress Gateway
  ↓
Allow-list
  ↓
Approved endpoint
```

This can be used where organizations need limited internal/external connectivity.

## Default-deny model

Everything starts denied:

```text
agent → Internet = DENY
agent → DNS = DENY
agent → unknown IP = DENY
agent → unknown port = DENY
```

Only explicitly approved routes are allowed.

## Network identities

Associate connections with:

```text
task_id
agent_id
container_id
user_id
destination
port
policy_decision_id
bytes_sent
bytes_received
timestamp
```

## Zero-egress demonstration

For the prototype, visibly show:

```text
Task running
     ↓
Agent invokes tools
     ↓
Network monitor active
     ↓
attempts counter = 0
blocked attempts = N
allowed external bytes = 0
```

This is much stronger than merely saying "the system is offline."

## Firewall

Use host/container-level rules such as nftables or an equivalent policy mechanism.

Conceptually:

```text
OUTPUT
  ↓
DENY
  ↓
explicit rules for:
  model server
  internal DB
  vector store
  object store
  approved services
```

## DNS control

Prefer an internal DNS service.

An unknown hostname should not resolve to a public destination.

## Network logging

Record:

```text
CONNECT_ATTEMPT
CONNECT_ALLOWED
CONNECT_BLOCKED
DNS_QUERY
DNS_BLOCKED
```

## Important distinction

A network monitor is not the same as a network boundary.

The architecture should contain both:

```text
enforcement
+
independent monitoring
```

That gives evidence when enforcement is working.

## Prototype test

Run a sandboxed agent that intentionally tries:

```python
requests.get("https://example.com")
```

Expected:

```text
connection blocked
NETWORK_BLOCKED event generated
task remains inside sandbox
```

Then run a legitimate internal call and show:

```text
internal database = allowed
Internet = blocked
```
