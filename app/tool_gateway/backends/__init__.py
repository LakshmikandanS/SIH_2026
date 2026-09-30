"""Tool backends.

Nothing here registers itself on import. `app.tool_gateway.registry` is the
only table, and a backend is attached to a tool name by an explicit
`register_backend()` call made by the step that owns that tool.
"""
