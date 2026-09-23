"""Routes for the sandbox service. Thin: verification and execution live in
citadel_tools.sandbox, the probe in citadel_sovereignty."""

from __future__ import annotations

import os
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from citadel_contracts.receipts import InMemoryNonceStore
from citadel_platform.keyring import load_receipt_public_key
from citadel_sovereignty import EgressRecorder, run_probe
from citadel_tools.sandbox import run_verified

KIND = os.environ.get("CITADEL_SANDBOX_KIND", "container")


def create_app() -> Starlette:
    public_key = load_receipt_public_key()
    nonces = InMemoryNonceStore()
    # The sandbox has no database: its probe outcomes travel back in the reply and the
    # caller records them with attribution (the recorder here only counts).
    recorder = EgressRecorder(None, "sandbox")
    state: dict[str, Any] = {"runs": 0, "refused": 0}

    async def health(request: Request) -> Response:
        return JSONResponse({
            "ok": True,
            "service": "citadel-sandbox",
            "kind": KIND,
            "runs": state["runs"],
            "refused_receipts": state["refused"],
            "network": "internal-only network, no route out" if KIND == "container" else "host process",
        })

    async def run(request: Request) -> Response:
        body: Any = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"receipt_verified": False, "receipt_error": "request must be a JSON object"}, status_code=400)
        result = await run_in_threadpool(run_verified, body, public_key=public_key, nonces=nonces, kind=KIND)
        if result.get("receipt_verified"):
            state["runs"] += 1
        else:
            state["refused"] += 1
        return JSONResponse(result)

    async def probe(request: Request) -> Response:
        body: Any = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        task_id = body.get("task_id") if isinstance(body, dict) else None
        report = await run_in_threadpool(run_probe, recorder, task_id=None)
        report["task_id"] = task_id
        return JSONResponse(report)

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/run", run, methods=["POST"]),
        Route("/probe", probe, methods=["POST"]),
    ])


__all__ = ["create_app", "KIND"]
