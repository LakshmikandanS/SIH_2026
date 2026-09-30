"""Operator Console -- a thin browser front end over the six existing HTTP
surfaces (design doc sections 6.1, 6.2, 6.8, 6.10).

**Scope note.** Section 10 defers "a polished dashboard", and this is not one:
it adds no server capability, no new endpoint, and no state of its own. It is
a single static page that calls exactly the same API the `citadel` CLI calls,
added at the project owner's request so the slice can be driven and tested in
a browser rather than only from a shell.

In particular it resolves an `approval_id` from a task's own trace
(`APPROVAL_REQUESTED`), the same way `cli/client.py::find_approval_id` does,
precisely so that no `GET /approvals/...` lookup endpoint has to exist.
"""

from app.ui.router import router

__all__ = ["router"]
