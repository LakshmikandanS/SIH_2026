"""HTTP surface (design doc section 6.1, section 6.2, C-005).

    "`/task "..." --classification CONFIDENTIAL`" (section 1.1 step 2) ->
    `POST /task` here, the Query Router role folded into the Orchestrator
    for this MVP (this step's own brief: "it is not a separate service").

    "Synchronous `POST /internal/orchestrate`" (section 6.2) -> its own
    endpoint below, even though `/task` calls the same `orchestrate()`
    function directly (a Python call, not a second HTTP hop) since both
    roles share one trusted-zone process (section 2).

    "`GET /tasks/{id}` and `GET /tasks/{id}/trace` are served *only* by the
    Orchestrator" (section 6.2, resolving C-005) -- both are below, and
    nowhere else in this codebase defines a route matching either path
    (Control Plane's routers are `app/identity/router.py` and
    `app/policy/router.py`; neither mounts `/tasks/...`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.db.engine import SessionLocal
from app.db.models import Task, User
from app.db.state_machines import Classification
from app.identity.dependencies import current_identity
from app.identity.tokens import SessionIdentity
from app.ids import new_id
from app.ids import TASK as TASK_ID_PREFIX
from app.observability import EventType, append_event, format_trace, get_trace
from app.orchestrator.errors import TASK_ALREADY_RUNNING, OrchestrationError
from app.orchestrator.schemas import OrchestratePayload, TaskCreateRequest
from app.orchestrator.service import orchestrate

router = APIRouter(tags=["orchestrator"])

#: section 6.2's fixed handoff for this slice's one scenario -- there is no
#: general task-type dispatcher (section 5.2: "no planning algorithm beyond
#: this exists for the MVP").
_DEFAULT_TASK_TYPE = "DOCUMENT_ANALYSIS"
_DEFAULT_REQUIREMENTS = {"needs_rag": True, "needs_document_generation": True}


def _known_classification(value: str) -> bool:
    try:
        Classification.rank(value)
        return True
    except ValueError:
        return False


def _result_body(result) -> dict:
    return {
        "task_id": result.task_id,
        "status": result.status,
        "agent_status": result.agent_status,
        "artifact_id": result.artifact_id,
        "reason": result.reason,
    }


@router.post("/task", status_code=status.HTTP_201_CREATED)
def post_task(
    payload: TaskCreateRequest, identity: SessionIdentity = Depends(current_identity)
) -> dict:
    """The Query Router role (section 6.2 step 3-4): creates the `Task` row
    and returns `task_id` -- classification is rule-based, never inferred:
    the slash command implies class TASK, and `--classification` here is
    taken as authoritative exactly as given. Then, synchronously, hands off
    into the Orchestrator (section 1.1 steps 3-5)."""
    if not _known_classification(payload.classification):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "INVALID_CLASSIFICATION",
                    "message": f"{payload.classification!r} is not a known classification",
                }
            },
        )

    with SessionLocal() as session:
        user = session.get(User, identity.user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"code": "UNKNOWN_USER", "message": "session user not found"}},
            )
        task_id = new_id(TASK_ID_PREFIX)
        task = Task(
            task_id=task_id,
            user_id=user.user_id,
            # user-declared, authoritative -- never inferred or overridden.
            classification=payload.classification,
            requirements=dict(_DEFAULT_REQUIREMENTS),
        )
        session.add(task)
        session.commit()
        owner_id = user.user_id

    append_event(
        task_id,
        owner_id,
        EventType.TASK_CREATED,
        {
            "text": payload.text,
            "classification": payload.classification,
            "requirements": _DEFAULT_REQUIREMENTS,
        },
    )

    try:
        result = orchestrate(
            OrchestratePayload(
                task_id=task_id,
                user_id=owner_id,
                classification=payload.classification,
                task_type=_DEFAULT_TASK_TYPE,
                requirements=dict(_DEFAULT_REQUIREMENTS),
            )
        )
    except OrchestrationError as exc:
        code_status = (
            status.HTTP_409_CONFLICT
            if exc.code == TASK_ALREADY_RUNNING
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(
            status_code=code_status, detail={"error": {"code": exc.code, "message": exc.message}}
        ) from None

    return _result_body(result)


@router.post("/internal/orchestrate")
def post_internal_orchestrate(payload: OrchestratePayload) -> dict:
    """section 6.2's canonical handoff, callable directly. Not
    session-authenticated: this is the internal Query-Router-to-Orchestrator
    hop, which section 6.2 never asks to carry a Bearer token of its own --
    the acting user was already authenticated by `/task` above, and
    `payload.user_id` here is only ever used to look up `department` for
    capability scoping (`app.capability.issue_for_step`), never trusted as an
    authorization decision by itself."""
    try:
        result = orchestrate(payload)
    except OrchestrationError as exc:
        code_status = (
            status.HTTP_409_CONFLICT
            if exc.code == TASK_ALREADY_RUNNING
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(
            status_code=code_status, detail={"error": {"code": exc.code, "message": exc.message}}
        ) from None
    return _result_body(result)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, identity: SessionIdentity = Depends(current_identity)) -> dict:
    """The Orchestrator is the sole owner of task status (section 6.2/C-005)."""
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"code": "UNKNOWN_TASK", "message": f"no task {task_id!r}"}},
            )
        return {
            "task_id": task.task_id,
            "user_id": task.user_id,
            "classification": task.classification,
            "status": task.status,
            "requirements": task.requirements,
            "version": task.version,
            "created_at": task.created_at.isoformat(),
        }


@router.get("/tasks/{task_id}/trace")
def get_task_trace(task_id: str, identity: SessionIdentity = Depends(current_identity)) -> dict:
    """`/trace <task_id>` (section 1.1's closing line): the full ordered
    event log for one task, at any point."""
    events = get_trace(task_id)
    if not events:
        with SessionLocal() as session:
            if session.get(Task, task_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": {"code": "UNKNOWN_TASK", "message": f"no task {task_id!r}"}},
                )
    return {
        "task_id": task_id,
        "events": [
            {
                "event_id": event.event_id,
                "seq": event.seq,
                "event_type": event.event_type,
                "actor_id": event.actor_id,
                "payload": event.payload,
                "timestamp": event.timestamp.isoformat(),
                "previous_hash": event.previous_hash,
                "hash": event.hash,
            }
            for event in events
        ],
        "text": format_trace(events),
    }
