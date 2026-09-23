"""fs.read and fs.write: the task's own scratch workspace, and nothing outside it.

Paths come from a model, so every one is resolved under the task workspace root and
refused if it would land anywhere else. Reading a directory lists it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from citadel_contracts.domain import Resource
from citadel_platform.storage import PathEscape, scoped_path

from citadel_tools.context import Invocation, ResourceNotFound, ToolContext, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin, task_resource

READ_CHARS = 12_000
WRITE_BYTES = 1_000_000
LIST_LIMIT = 200


def workspace_path(ctx: ToolContext, relative: str) -> Path:
    relative = (relative or ".").strip() or "."
    try:
        return scoped_path(ctx.workspace(), relative)
    except PathEscape as exc:
        raise ToolFailure(f"{exc}; paths are relative to the task workspace") from None


def normalised(ctx: ToolContext, relative: str) -> str:
    path = workspace_path(ctx, relative)
    root = ctx.workspace().resolve()
    return "." if path == root else path.relative_to(root).as_posix()


def _resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    return task_resource(ctx, "workspace", normalised(ctx, str(args["path"])))


def listing(root: Path, base: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(base.rglob("*"))[:LIST_LIMIT]:
        if path.is_file():
            entries.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size})
    return entries


def _read(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    path = workspace_path(ctx, str(args["path"]))
    root = ctx.workspace().resolve()
    if path.is_dir():
        entries = listing(root, path)
        return ToolOutput({"path": normalised(ctx, str(args["path"])), "files": entries},
                          f"listed {len(entries)} file(s) in the workspace")
    if not path.exists():
        raise ResourceNotFound(f"{args['path']} does not exist in the task workspace")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ToolOutput(
            {"path": path.relative_to(root).as_posix(), "bytes": len(data), "binary": True,
             "note": "binary file; use sheet.read for spreadsheets"},
            f"{path.name} is binary ({len(data)} bytes)",
        )
    truncated = len(text) > READ_CHARS
    return ToolOutput(
        {"path": path.relative_to(root).as_posix(), "bytes": len(data), "content": text[:READ_CHARS], "truncated": truncated},
        f"read {path.name} ({len(data)} bytes)",
    )


def _write(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    path = workspace_path(ctx, str(args["path"]))
    if path == ctx.workspace().resolve():
        raise ToolFailure("fs.write needs a file path, not the workspace root")
    content = str(args["content"])
    encoded = content.encode("utf-8")
    if len(encoded) > WRITE_BYTES:
        raise ToolFailure(f"content is {len(encoded)} bytes; the workspace limit per file is {WRITE_BYTES}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    relative = path.relative_to(ctx.workspace().resolve()).as_posix()
    return ToolOutput({"path": relative, "bytes": len(encoded)}, f"wrote {relative} ({len(encoded)} bytes)")


PLUGINS = {
    "fs.read": ToolPlugin("fs.read", _resource, _read),
    "fs.write": ToolPlugin("fs.write", _resource, _write),
}

__all__ = ["PLUGINS", "workspace_path", "normalised", "listing"]
