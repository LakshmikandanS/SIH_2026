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


#: Every package name under packages/, derived from the directory listing rather than
#: hardcoded, so a tenth package added later is picked up without editing this file.
def package_names() -> list[str]:
    return sorted(
        p.name for p in (REPO_ROOT / "packages").iterdir() if (p / "src").is_dir()
    )


def all_source_files() -> list[Path]:
    """Every first-party Python source file across every package's src/, plus
    services/ once it has real code. Excludes tests, caches, venvs, and registry/
    (data, not code) -- this is the repo-wide counterpart to source_files()."""
    roots = [REPO_ROOT / "packages" / pkg / "src" for pkg in package_names()] + [
        REPO_ROOT / "services"
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            p
            for p in root.rglob("*.py")
            if ".venv" not in p.parts
            and "__pycache__" not in p.parts
            and "tests" not in p.parts
        )
    return files


def package_of(path: Path) -> str | None:
    """Which citadel package a source file belongs to, e.g. 'runtime' for
    packages/runtime/src/citadel_runtime/orchestrator.py. None for a path outside
    packages/ (e.g. services/)."""
    try:
        rel = path.relative_to(REPO_ROOT / "packages")
    except ValueError:
        return None
    return rel.parts[0]


def control(name: str) -> str:
    """Read a negative-control fixture as source text."""
    path = CONTROLS / f"{name}{CONTROL_SUFFIX}"
    if not path.exists():
        raise AssertionError(
            f"Negative control {path} is missing. A detector without a control is "
            f"a detector nobody has proven works."
        )
    return path.read_text(encoding="utf-8")
