"""Shared helpers for structural tests.

A structural test greps or walks the source tree for a pattern that must not appear.
Its danger is that it passes when the pattern is wrong, the glob stopped matching, or
someone deleted the body -- a test that cannot fail produces confidence rather than
safety.

So every detector is paired with a fixture it MUST catch, and asserts both directions.
See tests/structural/AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLS = Path(__file__).parent / "fixtures" / "negative_controls"

# Controls are stored as .txt so pytest never collects them and Python never imports
# them. They are read as text by the detector under test.
CONTROL_SUFFIX = ".py.txt"


def source_files(*, package: str | None = None) -> list[Path]:
    """Every first-party Python source file, optionally within one package."""
    root = REPO_ROOT / "packages" / package / "src" if package else REPO_ROOT
    return [
        p
        for p in root.rglob("*.py")
        if ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and "tests" not in p.parts
    ]


def control(name: str) -> str:
    """Read a negative-control fixture as source text."""
    path = CONTROLS / f"{name}{CONTROL_SUFFIX}"
    if not path.exists():
        raise AssertionError(
            f"Negative control {path} is missing. A detector without a control is "
            f"a detector nobody has proven works."
        )
    return path.read_text(encoding="utf-8")
