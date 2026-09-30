"""The `citadel` CLI (design doc §1, §6.1) -- step 9, `cli`.

This is the thin client the whole vertical slice is demoed through. It owns
no business logic: every command is a thin wrapper around one of the six real
HTTP endpoints the trusted-zone process (`app.main:create_app`) already
exposes -- `POST /login`, `POST /task`, `GET /tasks/{id}`,
`GET /tasks/{id}/trace`, `POST /approvals/{approval_id}/decision`, and
`POST /admin/tools/{tool_name}/disable`. No new server-side endpoint exists
for this package to call, and none should ever be added for it (see this
package's own module docstrings for where that line is drawn).

Run it as a module, e.g.::

    .venv/Scripts/python -m cli login
    .venv/Scripts/python -m cli task "..." --classification CONFIDENTIAL
    .venv/Scripts/python -m cli status <task_id>
    .venv/Scripts/python -m cli approve <approval_id_or_task_id>
    .venv/Scripts/python -m cli reject <approval_id_or_task_id> --comment "..."
    .venv/Scripts/python -m cli trace <task_id>
    .venv/Scripts/python -m cli admin disable-tool <tool_name>
    .venv/Scripts/python -m cli serve

No packaging/distribution is built for this MVP (non-goal) -- there is no
`citadel` console-script entry point, only this module.
"""

from __future__ import annotations
