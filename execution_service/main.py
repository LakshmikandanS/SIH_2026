"""The Execution Service's HTTP surface -- one endpoint, one job.

    POST /execute   {"code": "...", "execution_id": "...", "timeout_seconds": 10}
      -> {"success": true, "result": {"stdout": "...", "stderr": "...", "exit_code": 0},
          "error": null, "metadata": {"execution_id": "..."}}
      -> {"success": false, "result": null,
          "error": {"code": "EXECUTION_ERROR", "message": "..."},
          "metadata": {"execution_id": "..."}}

    GET /health     -> {"status": "ok", "docker": "reachable" | "unreachable: ..."}

This process is meant to sit behind a Docker network the sandbox containers
it spawns cannot see any other route on (`docker/docker-compose.yml`), and
that the Tool Gateway -- not an agent directly -- is the only client of. It
performs no identity/capability/policy check of its own: those already ran
in the trusted zone (§6.6 Steps A and B) before this HTTP call was made. Its
only job is Step C's actual execution, inside a real process/container
boundary.

Run standalone:

    .venv/Scripts/python -m execution_service
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from execution_service.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    ExecutionErrorBody,
    ExecutionResultBody,
)
from execution_service.sandbox import SandboxError, run_python

logger = logging.getLogger("execution_service")

#: The one error code this process ever reports -- deliberately the same
#: closed-set member the Tool Gateway already uses for a backend failure
#: (design doc §6.6), so nothing downstream needs to translate it.
_ERROR_CODE = "EXECUTION_ERROR"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Citadel Execution Service (isolated execution zone)",
        version="0.5.0",
        description=(
            "Holds the only Docker socket in the system (design doc §2). "
            "Runs one-shot, network-disabled containers and nothing else."
        ),
    )

    @app.get("/health")
    def health() -> dict:
        import docker
        from docker.errors import DockerException

        try:
            docker.from_env().ping()
            docker_status = "reachable"
        except DockerException as exc:  # pragma: no cover - environment dependent
            docker_status = f"unreachable: {exc}"
        return {"status": "ok", "docker": docker_status}

    @app.post("/execute", response_model=ExecuteResponse)
    def execute(payload: ExecuteRequest) -> JSONResponse:
        metadata = {"execution_id": payload.execution_id} if payload.execution_id else {}
        try:
            result = run_python(payload.code, timeout_seconds=payload.timeout_seconds)
        except SandboxError as exc:
            logger.warning("sandbox execution failed: %s", exc)
            body = ExecuteResponse(
                success=False,
                result=None,
                error=ExecutionErrorBody(code=_ERROR_CODE, message=str(exc)),
                metadata=metadata,
            )
            return JSONResponse(status_code=200, content=body.model_dump())

        body = ExecuteResponse(
            success=True,
            result=ExecutionResultBody(
                stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code
            ),
            error=None,
            metadata=metadata,
        )
        return JSONResponse(status_code=200, content=body.model_dump())

    return app


app = create_app()
