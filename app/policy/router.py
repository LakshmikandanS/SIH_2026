"""`POST /admin/tools/{tool_name}/disable` -- the one emergency control (§6.8).

Admin-role-only, "per §6.4's identity model" -- which means the role is read
from the verified session JWT and from nowhere else. A body claiming
`{"user_id": "...", "roles": ["admin"]}` changes nothing; see
`app.identity.dependencies`.

No re-enable endpoint exists. §6.8 names exactly one control.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.db.state_machines import Role
from app.identity.dependencies import (
    drop_client_identity,
    optional_json_body,
    require_role,
)
from app.identity.tokens import SessionIdentity
from app.observability import EventType, append_event
from app.policy.tool_disabled import get_registry
from app.policy.tools import ALLOWED_TOOLS, Tool

router = APIRouter(prefix="/admin", tags=["emergency-control"])

#: Every tool name the system knows about. Used only to tell an admin that
#: they may have typed the name wrong -- an unknown name is still accepted and
#: still flagged, because pre-emptively disabling a tool that has not been
#: registered yet is a legitimate thing to want.
_KNOWN_TOOLS = ALLOWED_TOOLS | {Tool.HOST_SHELL, Tool.ARTIFACT_RELEASE, Tool.ECHO}


class DisableToolResponse(BaseModel):
    tool: str
    disabled: bool
    previously_disabled: bool
    known_tool: bool


@router.post("/tools/{tool_name}/disable", response_model=DisableToolResponse)
async def post_disable_tool(
    tool_name: str,
    identity: SessionIdentity = Depends(require_role(Role.ADMIN)),
    body: dict[str, Any] = Depends(optional_json_body),
) -> DisableToolResponse:
    # §6.4, no exceptions: whatever identity the caller asserted in the body is
    # discarded here, in the open, before anything else happens. The acting
    # admin is `identity.user_id`, which came out of a verified signature.
    drop_client_identity(body)

    previously_disabled = get_registry().disable(tool_name)

    # System-scoped event: no task_id (the foundation writer allows a null
    # task_id precisely so an admin DISABLE TOOL joins the one chain), and
    # actor_id is the admin from the session.
    append_event(
        None,
        identity.user_id,
        EventType.POLICY_DECISION,
        {
            "control": "DISABLE_TOOL",
            "tool": tool_name,
            "disabled": True,
            "previously_disabled": previously_disabled,
            "known_tool": tool_name in _KNOWN_TOOLS,
            "reason": "tool disabled by administrator",
        },
    )

    return DisableToolResponse(
        tool=tool_name,
        disabled=True,
        previously_disabled=previously_disabled,
        known_tool=tool_name in _KNOWN_TOOLS,
    )
