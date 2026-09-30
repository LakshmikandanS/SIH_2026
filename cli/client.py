"""The CLI's one HTTP client. Every command in `cli/main.py` goes through
this module and nowhere else calls `httpx` directly -- the same "one
chokepoint" discipline the rest of this codebase uses for its own seams
(the Tool Gateway, the Observability writer).

Six endpoints, matching docs/BUILD_LOG.md's Step 4/7/8 contract exactly:

    POST /login
    POST /task
    GET  /tasks/{id}
    GET  /tasks/{id}/trace
    POST /approvals/{approval_id}/decision
    POST /admin/tools/{tool_name}/disable

`CitadelClient` accepts an optional pre-built `httpx.Client` (or FastAPI's
`TestClient`, which is one) so the test suite can drive these exact same code
paths against an in-process app instead of a real socket -- see
`tests/test_cli.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app import ids


class CitadelAPIError(Exception):
    """One HTTP call failed. Carries enough to print a clean message, never a
    raw stack trace, back at the command layer (`cli/main.py`)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    code = "HTTP_ERROR"
    message = response.text
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            # app/*/router.py's own shape: {"detail": {"error": {"code", "message"}}}
            error = detail["error"]
            code = str(error.get("code", code))
            message = str(error.get("message", message))
        elif isinstance(detail, list) and detail:
            # FastAPI/pydantic's own validation-error shape (a 422 that never
            # reached a router's own HTTPException, e.g. an unknown decision).
            code = "VALIDATION_ERROR"
            message = "; ".join(
                f"{'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg', '')}"
                for item in detail
            )
        elif isinstance(detail, str):
            message = detail
    raise CitadelAPIError(response.status_code, code, message)


@dataclass
class LoginResult:
    access_token: str
    token_type: str
    expires_at: str
    user_id: str
    roles: list[str]


class CitadelClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 180.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CitadelClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # -- §6.4 Identity ----------------------------------------------------
    def login(self, username: str, password: str) -> LoginResult:
        response = self._client.post(
            "/login", json={"username": username, "password": password}
        )
        _raise_for_error(response)
        body = response.json()
        return LoginResult(
            access_token=body["access_token"],
            token_type=body.get("token_type", "bearer"),
            expires_at=body["expires_at"],
            user_id=body["user_id"],
            roles=list(body["roles"]),
        )

    # -- §6.2 Orchestrator --------------------------------------------------
    def submit_task(self, token: str, text: str, classification: str) -> dict[str, Any]:
        """`POST /task` -- synchronous, runs plan -> agent loop -> tool calls
        to completion before returning (§6.2). Can take 10-30s+."""
        response = self._client.post(
            "/task",
            json={"text": text, "classification": classification},
            headers=self._headers(token),
        )
        _raise_for_error(response)
        return response.json()

    def get_status(self, token: str, task_id: str) -> dict[str, Any]:
        response = self._client.get(f"/tasks/{task_id}", headers=self._headers(token))
        _raise_for_error(response)
        return response.json()

    def get_trace(self, token: str, task_id: str) -> dict[str, Any]:
        response = self._client.get(f"/tasks/{task_id}/trace", headers=self._headers(token))
        _raise_for_error(response)
        return response.json()

    def find_approval_id(self, token: str, task_id: str) -> str:
        """Resolve a task's approval id from its own trace's
        `APPROVAL_REQUESTED` event (`app/artifact/pipeline.py`'s own emitter)
        -- there is no `GET /approvals/...` lookup endpoint (§6.2/C-005 name
        exactly six HTTP surfaces and this step adds none)."""
        trace = self.get_trace(token, task_id)
        for event in trace["events"]:
            if event["event_type"] == "APPROVAL_REQUESTED":
                approval_id = event["payload"].get("approval_id")
                if approval_id:
                    return approval_id
        raise CitadelAPIError(
            404,
            "NO_APPROVAL_FOUND",
            f"task {task_id!r} has no APPROVAL_REQUESTED event in its trace yet",
        )

    def resolve_approval_id(self, token: str, id_or_task_id: str) -> str:
        """Accept either an approval id (`APR...`) or a task id (`T...`) --
        the CLI's own convenience, not a server capability. A task id is
        resolved via `find_approval_id` above; anything else is passed
        through unchanged and left to the decision endpoint to validate."""
        if id_or_task_id.startswith(ids.TASK):
            return self.find_approval_id(token, id_or_task_id)
        return id_or_task_id

    # -- §6.10 Approval -------------------------------------------------------
    def decide_approval(
        self, token: str, approval_id: str, decision: str, comment: Optional[str]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": decision, "comment": comment},
            headers=self._headers(token),
        )
        _raise_for_error(response)
        return response.json()

    # -- §6.8 Emergency control -----------------------------------------------
    def disable_tool(self, token: str, tool_name: str) -> dict[str, Any]:
        response = self._client.post(
            f"/admin/tools/{tool_name}/disable",
            json={"disabled": True},
            headers=self._headers(token),
        )
        _raise_for_error(response)
        return response.json()
