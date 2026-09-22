"""`citadel_contracts` imports nothing internal -- checked from the repo-wide vantage
point, walking the real tree from root.

`packages/contracts/tests/test_contracts_is_self_contained.py` checks the same rule
from a different vantage point: provable with `citadel_contracts` as the only package
present, no rest of the repo. This file is the repo-integrated counterpart, wired into
the same suite as every other package's boundary. Deliberately a different basename --
MONARCH's own `pyproject.toml` documents a real pytest collection error from two test
files sharing a basename with no `__init__.py` to disambiguate, which is exactly the
situation here (`tests/structural/` has none). Neither file substitutes for the other.

AST-based: walks imports rather than grepping, so an aliased `import citadel_platform as p`
is still caught.
"""

from __future__ import annotations

import ast

from conftest import REPO_ROOT, control, source_files


def _imported_citadel_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root.startswith("citadel_"):
                    roots.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0 and node.module.startswith("citadel_"):
                roots.add(node.module.split(".")[0])
            # node.level > 0 is a relative import -- stays inside contracts/, not a
            # boundary crossing.
    return roots


def _offending(source: str) -> set[str]:
    return _imported_citadel_roots(source) - {"citadel_contracts"}


def test_contracts_source_imports_no_other_citadel_package():
    contracts_root = REPO_ROOT / "packages" / "contracts" / "src"
    offenders = []
    for path in source_files(package="contracts"):
        offending = _offending(path.read_text(encoding="utf-8"))
        if offending:
            offenders.append(f"{path.relative_to(contracts_root)} imports {sorted(offending)}")

    assert offenders == [], (
        "citadel_contracts must import nothing from any other Citadel package: "
        + "; ".join(offenders)
    )


def test_the_detector_catches_the_negative_control():
    offending = _offending(control("contracts_importing_platform"))
    assert offending == {"citadel_platform"}, (
        f"expected exactly one offending root (citadel_platform), got {offending}"
    )


def test_the_detector_is_not_vacuous():
    """A relative import and an unrelated stdlib import must not be flagged --
    otherwise this detector would flag everything and "catching" the control would
    prove nothing."""
    assert _offending("from .receipts import DecisionReceipt\n") == set()
    assert _offending("import json\nfrom citadel_contracts.domain import Task\n") == set()
