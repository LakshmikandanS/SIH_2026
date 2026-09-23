"""The container image has everything the services import.

The image (`ops/compose/Dockerfile`) does not pip-install the workspace packages. It
installs `ops/compose/requirements.txt` and puts every `packages/*/src` and
`services/*/src` on `PYTHONPATH`, exactly as `scripts/lib/env.sh` does in a sandbox. That
leaves two ways to ship an image that crashes on start: a package begins importing a new
third-party module that `requirements.txt` never gains, or a new workspace package never
reaches the Dockerfile's `PYTHONPATH`. The environment this repository is built in cannot
run Docker at all, so nothing but these tests connects the code to the image before it
reaches the demonstration machine.

Each package must also *declare* what it imports, so a `uv sync` environment and the
image agree about what is installed.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[2]

# Import name -> distribution name, where they differ.
_DIST_OF = {"yaml": "pyyaml", "jwt": "pyjwt", "docx": "python-docx", "PIL": "pillow", "multipart": "python-multipart"}
# citadel_platform.audit.postgres: the psycopg audit writer, an optional driver that no
# service imports (they use the psql-subprocess client); see that module's docstring.
_OPTIONAL = frozenset({"psycopg"})


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    return _norm(re.split(r"[<>=!~\[ ;]", spec.strip(), maxsplit=1)[0])


def third_party_imports(sources: Iterable[str]) -> set[str]:
    """Top-level distribution names imported by absolute imports in `sources`."""
    found: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top == "__future__" or top.startswith("citadel_"):
                    continue
                found.add(_norm(_DIST_OF.get(top, top)))
    return found - _OPTIONAL


def problems(package: str, sources: Iterable[str], manifest: str, requirements: str) -> list[str]:
    declared = {_requirement_name(d) for d in tomllib.loads(manifest)["project"].get("dependencies", [])}
    pinned = {
        _requirement_name(line) for line in requirements.splitlines() if line.strip() and not line.lstrip().startswith("#")
    }
    found = []
    for dist in sorted(third_party_imports(sources)):
        if dist not in declared:
            found.append(f"{package} imports {dist} but its pyproject.toml does not declare it")
        if dist not in pinned:
            found.append(f"{package} imports {dist} but ops/compose/requirements.txt does not install it")
    return found


def _workspace_packages() -> list[Path]:
    return sorted(p.parent for p in [*REPO.glob("packages/*/pyproject.toml"), *REPO.glob("services/*/pyproject.toml")])


def test_every_third_party_import_is_declared_and_installed_in_the_image():
    requirements = (REPO / "ops" / "compose" / "requirements.txt").read_text(encoding="utf-8")
    found: list[str] = []
    for package in _workspace_packages():
        sources = [p.read_text(encoding="utf-8") for p in sorted((package / "src").rglob("*.py"))]
        manifest = (package / "pyproject.toml").read_text(encoding="utf-8")
        found += problems(str(package.relative_to(REPO)), sources, manifest, requirements)
    assert found == []


def test_the_image_puts_every_workspace_package_on_its_path():
    dockerfile = (REPO / "ops" / "compose" / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"PYTHONPATH=(\S+)", dockerfile)
    assert match, "the Dockerfile no longer sets PYTHONPATH"
    on_path = set(match.group(1).split(":"))
    wanted = {f"/app/{package.relative_to(REPO).as_posix()}/src" for package in _workspace_packages()}
    assert sorted(wanted - on_path) == []


def test_the_check_catches_an_import_nobody_installs():
    manifest = '[project]\nname = "x"\ndependencies = ["pyyaml>=6", "citadel-platform"]\n'
    found = problems("packages/x", ["import yaml\nfrom numpy import array\n"], manifest, "PyYAML>=6.0,<7\n")
    assert found == [
        "packages/x imports numpy but its pyproject.toml does not declare it",
        "packages/x imports numpy but ops/compose/requirements.txt does not install it",
    ]


def test_the_check_is_not_vacuous():
    manifest = '[project]\nname = "x"\ndependencies = ["python-docx>=1.1", "pillow"]\n'
    sources = ["import os, json\nfrom docx import Document\nfrom PIL import Image\nfrom citadel_platform.db import Database\n"]
    assert third_party_imports(sources) == {"python-docx", "pillow"}
    assert problems("packages/x", sources, manifest, "python-docx>=1.1,<2\n# a comment\npillow>=10.4\n") == []
