"""Control Plane -- Policy Engine (§6.7) and the one emergency control (§6.8).

Four static rules in a fixed order, first match wins, default DENY. No policy
DSL, no RBAC x ABAC combinator -- see `app/policy/engine.py` for why the order
is load-bearing.
"""

from app.policy.context import (
    PolicyAction,
    PolicyAgent,
    PolicyResource,
    PolicyTask,
    PolicyUser,
)
from app.policy.engine import Decision, PolicyOutcome, Rule, decide, evaluate
from app.policy.tool_disabled import (
    ToolDisabledRegistry,
    get_registry,
    set_registry,
    tool_disabled,
)
from app.policy.tools import ALLOWED_TOOLS, Tool

__all__ = [
    "ALLOWED_TOOLS",
    "Decision",
    "PolicyAction",
    "PolicyAgent",
    "PolicyOutcome",
    "PolicyResource",
    "PolicyTask",
    "PolicyUser",
    "Rule",
    "Tool",
    "ToolDisabledRegistry",
    "decide",
    "evaluate",
    "get_registry",
    "set_registry",
    "tool_disabled",
]
