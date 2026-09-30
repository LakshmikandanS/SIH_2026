"""contracts -- the shared language, and the ONLY thing services share.

Citadel Target Architecture §4: "no service imports another service. Shared
meaning lives in `contracts/` and nowhere else." This package holds the
classification lattice, the three state machines, the plain domain schemas,
the closed event-type vocabulary, the uniform tool result envelope, and the
signed Decision Receipt.

`contracts/` imports nothing from the rest of the repository, ever
(`tests/invariants/test_contracts_import_boundary.py` proves it). Everything
in it is either stdlib or one of `pyjwt` / `cryptography`, both already a
dependency of the trusted workflow zone (`requirements-dev.txt`).

Re-exports the public name from each submodule so a caller writes
`from contracts import Classification, DecisionReceipt` rather than reaching
into individual files -- the same convention `app/observability/__init__.py`
and `app/capability/__init__.py` already use.
"""

from __future__ import annotations

from contracts.classification import Classification
from contracts.domain import (
    Agent,
    Approval,
    Artifact,
    Evidence,
    Resource,
    Task,
    User,
)
from contracts.envelopes import ErrorCode, UnknownErrorCode, failure, success
from contracts.events import EventType, UnknownEventType
from contracts.receipts import (
    DecisionReceipt,
    InMemoryNonceStore,
    NonceStore,
    ReceiptExpired,
    ReceiptInvalid,
    resource_digest,
    sign_receipt,
    verify_receipt,
)
from contracts.state_machines import (
    AGENT,
    APPROVAL,
    ARTIFACT,
    TASK,
    AgentStatus,
    AgentType,
    ApprovalState,
    ArtifactStatus,
    IllegalTransition,
    Role,
    StateMachine,
    TaskStatus,
)

__all__ = [
    "Classification",
    "User",
    "Task",
    "Agent",
    "Artifact",
    "Approval",
    "Evidence",
    "Resource",
    "ErrorCode",
    "UnknownErrorCode",
    "success",
    "failure",
    "EventType",
    "UnknownEventType",
    "DecisionReceipt",
    "resource_digest",
    "sign_receipt",
    "verify_receipt",
    "NonceStore",
    "InMemoryNonceStore",
    "ReceiptInvalid",
    "ReceiptExpired",
    "StateMachine",
    "IllegalTransition",
    "TaskStatus",
    "TASK",
    "ArtifactStatus",
    "ARTIFACT",
    "ApprovalState",
    "APPROVAL",
    "AgentStatus",
    "AGENT",
    "AgentType",
    "Role",
]
