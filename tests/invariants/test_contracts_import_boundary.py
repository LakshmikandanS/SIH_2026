"""Invariant 1 of P1 §7: `contracts/` imports nothing from `services/`,
`app/`, `cli/`, or `execution_service/`.

This is the repo-integrated counterpart to
`contracts/tests/test_contracts_is_self_contained.py`, which proves the same
rule with `contracts/` as the only thing present. This file instead walks
the real, live tree -- exactly the way
`tests/test_security.py::test_execution_zone_has_no_code_path_to_the_docker_socket`
already walks `app/` to prove its own boundary -- and ships the same kind of
negative control that file's own detector carries, so a passing result here
means the check ran, not that it was silently skipped.

Invariants 2-4 of P1 §7 (exactly one module signs a receipt; no component but
Control reads a private key; every registered tool backend verifies before
touching its resource) are not in this file. Each needs machinery this phase
has not built yet -- `decide_and_issue` (step 3), the key-loading code (step
2), and registered backends that call `verify_receipt` (steps 5-7) -- and
P1 §6 places them at step 8, once there is something real to check. Adding
them here now would either import code that does not exist yet or pass
vacuously; neither is protection.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The repo's own service-shaped top-level packages -- today's `app/` and
#: `execution_service/`, and the target's `services/` and `clients/` (which
#: `cli/` becomes under Target Architecture §4). `contracts/` reaching any of
#: these is exactly the boundary crossing P1 §1 rules out: "no service
#: imports another service. Shared meaning lives in contracts/."
_SERVICE_SHAPED_ROOTS = frozenset({"app", "execution_service", "services", "clients", "cli"})


def _imported_module_roots(source: str) -> set[str]:
    """Top-level package names named by any `import`/`from ... import` in
    `source`. A relative import (`from .receipts import x`, level > 0) stays
    inside the same package and is excluded -- `contracts/` referencing
    itself is not a boundary crossing."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def test_contracts_has_no_code_path_into_a_service_package():
    offenders = []
    for path in (_REPO_ROOT / "contracts").rglob("*.py"):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        hit = _imported_module_roots(path.read_text(encoding="utf-8")) & _SERVICE_SHAPED_ROOTS
        if hit:
            offenders.append(f"{rel} imports {sorted(hit)}")

    assert offenders == [], (
        "contracts/ has a code path into a service package: " + "; ".join(offenders) +
        ". contracts/ must import nothing but the standard library, pyjwt and "
        "cryptography -- see contracts/__init__.py."
    )


def test_the_contracts_import_boundary_detector_is_not_vacuous():
    """Negative control: prove the detector catches a planted violation
    rather than passing because it checks nothing -- the same discipline
    `test_the_docker_socket_detector_is_not_vacuous` applies to its own
    detector, immediately above the check it protects in that file."""
    assert "app" in _imported_module_roots("from app.db.engine import SessionLocal\n")
    assert "execution_service" in _imported_module_roots("import execution_service.main\n")
    assert "services" in _imported_module_roots("from services.control import policy\n")
    assert "cli" in _imported_module_roots("import cli\n")
    assert _imported_module_roots(
        "import json\nimport jwt\nfrom contracts.domain import Task\n"
        "from .receipts import DecisionReceipt\n"
    ).isdisjoint(_SERVICE_SHAPED_ROOTS)
