"""code.run: model-authored Python, executed only in the sandbox, only on a receipt.

The receipt is bound to the exact source text. The sandbox recomputes that binding
from the source it receives and refuses anything else, so the code that runs is
provably the code that was authorised. The task's workspace files travel with the
request (the sandbox mounts nothing from the host) and whatever the run writes comes
back into the workspace. The printed result becomes citable evidence (C1, C2...), and a
script that ran cleanly is kept as an artifact beside the task's other outputs.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from citadel_contracts.domain import Resource
from citadel_contracts.state_machines import ArtifactStatus
from citadel_deliverables import store_artifact, transition
from citadel_knowledge import register_computation
from citadel_platform.storage import safe_filename

from citadel_tools.context import Invocation, ReceiptRejected, ToolContext, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin
from citadel_tools.sandbox import code_resource

INPUT_LIMIT_BYTES = 5_000_000
INPUT_LIMIT_FILES = 20


def _resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    return code_resource(str(args["source"]), ctx.task_classification, (ctx.department,))


def _inputs(workspace: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    total = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or len(files) >= INPUT_LIMIT_FILES:
            continue
        size = path.stat().st_size
        if total + size > INPUT_LIMIT_BYTES:
            continue
        files[path.relative_to(workspace).as_posix()] = base64.b64encode(path.read_bytes()).decode("ascii")
        total += size
    return files


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _run(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    if ctx.sandbox is None:
        raise ToolFailure("no sandbox is configured on this deployment")
    source = str(args["source"])
    workspace = ctx.workspace()
    request = {
        "operation": "code.run",
        "source": source,
        "timeout_s": int(args.get("timeout_s") or 30),
        "files": _inputs(workspace),
        "receipt": invocation.receipt,
        "resource": {"classification": invocation.resource.classification, "acl": list(invocation.resource.acl)},
        "task_id": ctx.task_id,
    }
    ctx.emit(kind="sandbox", state="running", sandbox=ctx.sandbox.kind)
    response = ctx.sandbox.run(request)
    if not response.get("receipt_verified"):
        raise ReceiptRejected(str(response.get("receipt_error") or "the sandbox did not verify the receipt"))
    invocation.verified = True
    invocation.verified_by = f"sandbox ({ctx.sandbox.kind})"

    written: list[str] = []
    root = workspace.resolve()
    for name, encoded in (response.get("files") or {}).items():
        target = (root / name).resolve()
        if root not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded))
        written.append(target.relative_to(root).as_posix())

    exit_code = int(response.get("exit_code", -1))
    stdout = str(response.get("stdout") or "")
    stderr = str(response.get("stderr") or "")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    evidence: list[str] = []
    artifacts: list[str] = []
    if exit_code == 0:
        evidence_id = register_computation(
            ctx.db,
            ctx.task_id,
            text=f"Python script {digest[:12]} ran in the sandbox (exit 0) and printed: {stdout.strip()[-800:]}",
            classification=ctx.task_classification,
            detail={
                "tool": "code.run",
                "expression": f"python script sha256:{digest[:12]}",
                "result": _last_line(stdout),
                "source_sha256": digest,
                "stdout": stdout[-4000:],
                "sandbox": response.get("sandbox"),
            },
        )
        evidence.append(evidence_id)
        artifact_id, _ = store_artifact(
            ctx.db,
            ctx.data_dir,
            task_id=ctx.task_id,
            kind="py",
            title=f"Sandboxed script {digest[:12]}",
            filename=safe_filename(f"script-{digest[:12]}.py"),
            data=source.encode("utf-8"),
            classification=ctx.task_classification,
            created_by=ctx.user.user_id,
            verification={
                "passed": True,
                "tiers": [{"tier": 1, "name": "executed", "status": "pass",
                           "issues": [], "details": {"exit_code": 0, "duration_ms": response.get("duration_ms")}}],
                "sandbox": response.get("sandbox"),
            },
            provenance={"decision_id": invocation.decision_id, "stdout": stdout[-4000:], "files_written": written,
                        "evidence_id": evidence_id},
        )
        transition(ctx.db, artifact_id, ArtifactStatus.CANDIDATE)
        transition(ctx.db, artifact_id, ArtifactStatus.VERIFIED)
        artifacts.append(artifact_id)

    data: dict[str, Any] = {
        "exit_code": exit_code,
        "timed_out": bool(response.get("timed_out")),
        "stdout": stdout[-6000:],
        "stderr": stderr[-3000:],
        "files_written": written,
        "duration_ms": response.get("duration_ms"),
        "sandbox": (response.get("sandbox") or {}).get("isolation"),
    }
    if evidence:
        data["evidence_id"] = evidence[0]
        data["how_to_cite"] = f"Cite values this run printed as [{evidence[0]}]."
    elif exit_code != 0:
        data["next"] = "The script failed; read stderr, fix the code and run it again."
    outcome = "timed out" if response.get("timed_out") else f"exit {exit_code}"
    return ToolOutput(
        data=data,
        summary=f"ran {len(source.splitlines())} line(s) of Python in the {ctx.sandbox.kind} sandbox: {outcome}"
        + (f"; wrote {', '.join(written)}" if written else ""),
        evidence=evidence,
        artifacts=artifacts,
        detail={"source": source[:20000], "sandbox": response.get("sandbox")},
    )


PLUGINS = {"code.run": ToolPlugin("code.run", _resource, _run)}

__all__ = ["PLUGINS"]
