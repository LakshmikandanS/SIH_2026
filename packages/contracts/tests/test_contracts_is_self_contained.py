"""`citadel_contracts` imports nothing from the rest of the repository, ever.

Root AGENTS.md: "no package imports another package's internals; shared
meaning lives in citadel_contracts and nowhere else." A structural test
enforces this from the first commit -- it is the rule that makes the
layered architecture possible, and it is cheapest to hold from the
beginning.

This is the version of that check `citadel_contracts` carries on its own,
provable with this package as the *only* thing present -- no other Citadel
package, nothing but this package and its own test dependencies (pytest,
pyjwt, cryptography). That is a stronger claim than "the live repo tree has
no offending import today": it is what makes "citadel_contracts tests pass
standalone" mean something.

The repo-integrated counterpart, which walks the real tree and ships a
negative control in the house style established here, is
`tests/structural/test_module_boundaries.py` (Task 17, not yet written).
This file and that one check the same rule from two different vantage
points; neither substitutes for the other -- see
packages/contracts/AGENTS.md.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Everything `citadel_contracts` is allowed to import: the standard library
#: (any root not in this set is assumed third-party or first-party and is
#: checked further below), plus its two declared third-party dependencies,
#: plus itself.
_ALLOWED_THIRD_PARTY = frozenset({"jwt", "cryptography"})
_ALLOWED_FIRST_PARTY = frozenset({"citadel_contracts"})

#: Test files additionally get pytest -- a test dependency, never imported
#: by the package's own runtime code.
_ALLOWED_IN_TESTS_ONLY = frozenset({"pytest"})

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "citadel_contracts"
_TESTS_ROOT = Path(__file__).resolve().parent


def _stdlib_roots() -> frozenset[str]:
    # Python 3.10+: the interpreter's own list of standard-library module
    # names. Falls back to a conservative subset if unavailable, so this
    # test degrades rather than silently passing on an older interpreter.
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    return frozenset(
        {
            "__future__", "abc", "ast", "collections", "dataclasses", "datetime",
            "hashlib", "json", "os", "pathlib", "secrets", "sys", "threading",
            "time", "typing", "uuid",
        }
    )


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
            # node.level > 0 is a relative import (`from . import x` /
            # `from .receipts import y`) -- stays inside citadel_contracts/
            # by construction and is not a boundary crossing.
    return roots


def _offending_roots(source: str, *, allow_pytest: bool) -> set[str]:
    stdlib = _stdlib_roots()
    allowed = _ALLOWED_THIRD_PARTY | _ALLOWED_FIRST_PARTY
    if allow_pytest:
        allowed = allowed | _ALLOWED_IN_TESTS_ONLY
    return _imported_roots(source) - stdlib - allowed


def test_contracts_package_files_import_only_stdlib_jwt_and_cryptography():
    offenders = []
    for path in sorted(_PACKAGE_ROOT.glob("*.py")):
        offending = _offending_roots(path.read_text(encoding="utf-8"), allow_pytest=False)
        if offending:
            offenders.append(f"{path.name} imports {sorted(offending)}")

    assert offenders == [], (
        "citadel_contracts must import nothing beyond the standard library, "
        "jwt and cryptography: " + "; ".join(offenders)
    )


def test_contracts_test_files_import_only_stdlib_jwt_cryptography_and_pytest():
    offenders = []
    for path in sorted(_TESTS_ROOT.glob("*.py")):
        offending = _offending_roots(path.read_text(encoding="utf-8"), allow_pytest=True)
        if offending:
            offenders.append(f"tests/{path.name} imports {sorted(offending)}")

    assert offenders == [], (
        "citadel_contracts/tests must import nothing beyond the standard "
        "library, jwt, cryptography and pytest: " + "; ".join(offenders)
    )


def test_the_detector_is_not_vacuous():
    """Negative control: prove the detector catches a planted violation
    rather than passing because it checks nothing."""
    assert "citadel_platform" in _offending_roots(
        "from citadel_platform.db import SessionLocal\n", allow_pytest=False
    )
    assert "citadel_runtime" in _offending_roots(
        "import citadel_runtime.orchestrator\n", allow_pytest=False
    )
    assert _offending_roots(
        "import json\nimport jwt\nfrom citadel_contracts.domain import Task\n",
        allow_pytest=False,
    ) == set()
    # A relative import within citadel_contracts/ itself is not a boundary crossing.
    assert _offending_roots("from .receipts import DecisionReceipt\n", allow_pytest=False) == set()
    # pytest is disallowed for package files but allowed for test files.
    assert "pytest" in _offending_roots("import pytest\n", allow_pytest=False)
    assert "pytest" not in _offending_roots("import pytest\n", allow_pytest=True)
