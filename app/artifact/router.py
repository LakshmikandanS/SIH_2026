"""`GET /artifacts/{artifact_id}` -- read a generated artifact.

Why this exists, given section 6.2/C-005 name a fixed set of HTTP surfaces:
section 6.10 puts a **human** approval gate in front of every release, and an
approver who cannot read the document they are releasing is not an approval
gate -- they would be rubber-stamping a hash. This route is the read half of
a control the design doc already requires; it grants no new authority.

It is read-only and adds no mutation path, so section 6.10's immutability rule
(`if artifact.status == RELEASED: reject()`) is untouched.

**Classification is enforced here, not assumed.** The artifact inherits its
task's classification, and a caller whose clearance sits below it is refused --
the same lattice comparison (`Classification.exceeds`) the Policy Engine and
the Data Plane use, never a string compare.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.db.engine import SessionLocal
from app.db.models import Artifact, Task, User
from app.db.state_machines import Classification
from app.identity.dependencies import current_identity
from app.identity.tokens import SessionIdentity

router = APIRouter(tags=["artifact"])


def _error(code_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=code_status, detail={"error": {"code": code, "message": message}})


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str, identity: SessionIdentity = Depends(current_identity)
) -> dict[str, Any]:
    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_ARTIFACT", f"no artifact {artifact_id!r}")

        task = session.get(Task, artifact.task_id)
        user = session.get(User, identity.user_id)
        if user is None:
            raise _error(status.HTTP_404_NOT_FOUND, "UNKNOWN_USER", "session user not found")

        # Fail closed: an unknown marking on either side is not comparable, so
        # it cannot be shown to be within clearance.
        try:
            too_high = Classification.exceeds(task.classification, user.clearance)
        except ValueError as exc:
            raise _error(status.HTTP_403_FORBIDDEN, "CLASSIFICATION_UNKNOWN", str(exc)) from None
        if too_high:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "CLASSIFICATION_EXCEEDS_CLEARANCE",
                f"artifact is {task.classification}; your clearance is {user.clearance}",
            )

        content = None
        read_error = None
        if artifact.path:
            try:
                content = Path(artifact.path).read_text(encoding="utf-8")
            except OSError as exc:
                read_error = str(exc)

        return {
            "artifact_id": artifact.artifact_id,
            "task_id": artifact.task_id,
            "version": artifact.version,
            "type": artifact.type,
            "status": artifact.status,
            "hash": artifact.hash,
            "provenance": artifact.provenance,
            "path": artifact.path,
            "classification": task.classification,
            "created_at": artifact.created_at.isoformat(),
            "content": content,
            "read_error": read_error,
        }
