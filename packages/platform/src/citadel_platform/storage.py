"""Where bytes live: one data directory, laid out by kind.

The database holds metadata and hashes; the bytes of uploaded documents, rendered page
images, generated artifacts and task workspaces live under `CITADEL_DATA_DIR`
(a named volume in Docker Compose, `.data/` in a checkout). Paths stored in the
database are always *relative* to that root, so the root can move between machines
without rewriting rows.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

DATA_DIR_VAR = "CITADEL_DATA_DIR"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, *, default: str = "file") -> str:
    """A filename that cannot climb out of its directory or confuse a shell."""
    base = os.path.basename(name.replace("\\", "/"))
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    return cleaned[:120] or default


class PathEscape(ValueError):
    """A relative path tried to leave the directory it is scoped to."""


@dataclass(frozen=True)
class DataDir:
    root: Path

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, default: Optional[Path] = None) -> "DataDir":
        environ = env if env is not None else os.environ
        value = environ.get(DATA_DIR_VAR)
        root = Path(value) if value else (default or Path.cwd() / ".data")
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root.resolve())

    def resolve(self, relative: str) -> Path:
        """An absolute path for a stored relative reference, refusing escapes."""
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathEscape(f"{relative!r} resolves outside the data directory")
        return candidate

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def document_dir(self, document_id: str, version: int) -> Path:
        path = self.root / "documents" / document_id / f"v{version}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_dir(self, artifact_id: str) -> Path:
        path = self.root / "artifacts" / artifact_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace(self, task_id: str) -> Path:
        path = self.root / "workspaces" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def scoped_path(root: Path, relative: str) -> Path:
    """`relative` resolved under `root`, or PathEscape. Used for task workspaces,
    where the relative path comes from a model and must never be trusted."""
    root = root.resolve()
    if os.path.isabs(relative) or relative.startswith(("/", "\\")):
        raise PathEscape(f"{relative!r} is absolute; workspace paths are relative")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathEscape(f"{relative!r} resolves outside the workspace")
    return candidate


__all__ = ["DATA_DIR_VAR", "DataDir", "PathEscape", "safe_filename", "scoped_path"]
