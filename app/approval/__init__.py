"""Step 8 -- Approval propagation (design doc section 6.10, BB-047).

    "An approval decision that never actually propagated to release
     anything." -- closed here: `POST /approvals/{approval_id}/decision` is
    the one transactional endpoint that moves `Approval`, `Artifact`, and
    `Task` together (on APPROVED) or hands off to the Orchestrator's one
    scoped revision (on REJECTED, section 5.3).

`app.main` mounts the router directly (`from app.approval.router import
router as approval_router`) -- this package's own `__init__.py` deliberately
does not re-export `router`, matching `app.orchestrator`/`app.policy`/
`app.identity`'s own convention: re-exporting a submodule's `router` under
the same name here would shadow the `app.approval.router` *submodule*
reference with the `APIRouter` instance on this package's namespace, which
breaks any later `import app.approval.router as ...`.
"""

from __future__ import annotations
