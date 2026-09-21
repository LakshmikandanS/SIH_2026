"""citadel_contracts -- the shared language, and the ONLY thing packages share.

Root `AGENTS.md`: no package imports another package's internals; shared
meaning lives in `citadel_contracts` and nowhere else. This package holds
the classification lattice, the three state machines, the plain domain
schemas, the open event-type registry, the uniform tool-result envelope,
and the signed Decision Receipt.

`citadel_contracts` imports nothing from any other Citadel package, ever
(`tests/structural/test_contracts_is_self_contained.py` proves it).
Everything in it is either stdlib or one of `pyjwt` / `cryptography`.

Re-exports the public name from each submodule so a caller writes
`from citadel_contracts import Classification, DecisionReceipt` rather than
reaching into individual files.
"""

from __future__ import annotations

from citadel_contracts.classification import Classification
from citadel_contracts.domain import (
    Agent,
    Approval,
    Artifact,
    Evidence,
    Resource,
    Task,
    User,
)
from citadel_contracts.envelopes import ErrorCode, UnknownErrorCode, failure, success
from citadel_contracts.events import EventRegistry, UnknownEventType
from citadel_contracts.receipts import (
    DecisionReceipt,
    InMemoryNonceStore,
    NonceStore,
    ReceiptExpired,
    ReceiptInvalid,
    resource_digest,
    sign_receipt,
    verify_receipt,
)
from citadel_contracts.state_machines import (
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
    "EventRegistry",
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
