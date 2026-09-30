"""Control Plane -- Capability issuance (design doc §6.5).

Signed (HMAC-SHA256), 5-minute, scoped to one operation and one task/agent
pair, issued once per plan step. No revocation list -- see `tokens.py`.
"""

from app.capability.service import CapabilityIssueError, issue_for_step
from app.capability.tokens import (
    Capability,
    CapabilityExpired,
    CapabilityInvalid,
    CapabilityScope,
    IssuedCapability,
    issue_capability,
    verify_capability,
)

__all__ = [
    "Capability",
    "CapabilityExpired",
    "CapabilityInvalid",
    "CapabilityIssueError",
    "CapabilityScope",
    "IssuedCapability",
    "issue_capability",
    "issue_for_step",
    "verify_capability",
]
