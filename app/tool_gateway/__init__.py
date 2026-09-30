"""Tool Gateway -- the authorization spine (design doc §6.6).

Every tool call in Citadel goes through `invoke()`. There is no second route:
backends are reachable only through `app.tool_gateway.registry`, and only
`gateway.invoke` reads that table.

Steps 5-8 attach a real backend like this, and change nothing else:

    from app.policy.tools import Tool
    from app.tool_gateway import register_backend
    register_backend(Tool.RAG_SEARCH, search_the_vector_store)
"""

from app.tool_gateway.envelope import (
    ALL_ERROR_CODES,
    ENVELOPE_KEYS,
    ErrorCode,
    UnknownErrorCode,
)
from app.tool_gateway.gateway import invoke, task_resource
from app.tool_gateway.registry import (
    ToolBackend,
    ToolNotRegistered,
    ToolRequest,
    clear_backends,
    get_backend,
    register_backend,
    registered_tools,
    unregister_backend,
)

__all__ = [
    "ALL_ERROR_CODES",
    "ENVELOPE_KEYS",
    "ErrorCode",
    "ToolBackend",
    "ToolNotRegistered",
    "ToolRequest",
    "UnknownErrorCode",
    "clear_backends",
    "get_backend",
    "invoke",
    "register_backend",
    "registered_tools",
    "task_resource",
    "unregister_backend",
]
