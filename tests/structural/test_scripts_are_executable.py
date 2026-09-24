"""Every shell script a person or a container is told to run is executable.

`scripts/run.sh` and `scripts/up.sh` were committed without their executable bit, so on
a Linux or WSL2 clone the README's `scripts/run.sh --fake-models` answered "Permission
denied". Git records the bit, but a checkout on a filesystem without one (the Windows
side of the repository) cannot show it, which is how it went unnoticed. This checks the
files as checked out. On Windows, which has no execute bit, `os.access` reports every
existing file as executable, so the check passes there without testing anything.

Sourced helpers (`scripts/lib/`) are not run, and are exempt.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIRS = ("scripts", "ops")


def not_executable(root: Path) -> list[str]:
    found = []
    for directory in SCRIPT_DIRS:
        for path in sorted((root / directory).rglob("*.sh")):
            relative = path.relative_to(root)
            if "lib" in relative.parts:  # sourced, never run
                continue
            if not os.access(path, os.X_OK):
                found.append(relative.as_posix())
    return found


@pytest.mark.structural
def test_every_runnable_shell_script_is_executable() -> None:
    assert not_executable(ROOT) == [], "run `git update-index --chmod=+x <script>` and commit"


@pytest.mark.structural
def test_negative_control_a_script_without_its_bit_is_caught(tmp_path: Path) -> None:
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    script = tmp_path / "scripts" / "run.sh"
    script.write_text("#!/usr/bin/env bash\necho started\n")
    script.chmod(0o644)
    helper = tmp_path / "scripts" / "lib" / "env.sh"
    helper.write_text("# sourced\n")
    helper.chmod(0o644)
    if os.access(script, os.X_OK):
        pytest.skip("this filesystem has no execute bit")
    assert not_executable(tmp_path) == ["scripts/run.sh"]
    script.chmod(0o755)
    assert not_executable(tmp_path) == []
