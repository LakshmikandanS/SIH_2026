"""Small helpers every route module shares: the app state, blocking calls off the event
loop, request bodies, and who may see a task."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, TypeVar

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from citadel_contracts.classification import Classification
from citadel_contracts.domain import User

from citadel_api.deps import AppState

T = TypeVar("T")


def state(request: Request) -> AppState:
    app_state: AppState = request.app.state.citadel
    return app_state


async def blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Every database call here is a psql subprocess: never on the event loop."""
    return await run_in_threadpool(fn, *args, **kwargs)


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body: Any = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def error(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def within_clearance(user: User, classification: str) -> bool:
    try:
        return not Classification.exceeds(str(classification).upper(), user.clearance.upper())
    except ValueError:
        return False


def can_view_task(user: User, task: Optional[Mapping[str, Any]]) -> bool:
    """The submitter always; an approver or admin for review, within their clearance."""
    if task is None:
        return False
    if task.get("submitted_by") == user.user_id:
        return True
    reviewer = bool({"approver", "admin"} & set(user.roles))
    return reviewer and within_clearance(user, str(task.get("classification") or ""))


def effective_level(user: User, requested: Optional[str]) -> str:
    """A requested classification, lowered to the user's clearance -- never raised."""
    clearance = user.clearance.upper()
    if not requested:
        return clearance
    level = str(requested).upper()
    try:
        return level if not Classification.exceeds(level, clearance) else clearance
    except ValueError:
        return clearance


__all__ = ["state", "blocking", "json_body", "error", "can_view_task", "within_clearance", "effective_level"]
