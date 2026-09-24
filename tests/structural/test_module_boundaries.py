"""The layer rule in root AGENTS.md's "Module map and the dependency rule" -- checked
against the real tree, package by package, so a forbidden edge is caught even though
every package's manifest declares only the dependencies the rule permits (the uv
workspace installs every member into one shared virtualenv, so the manifest alone
cannot stop a forbidden import at runtime -- see root AGENTS.md's own note on this).

AST-based: walks imports rather than grepping, so an aliased
`from citadel_knowledge import citations as c` is still caught.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control, package_of

#: The dependency rule, exactly as the root AGENTS.md diagram states it. `contracts`
#: permits nothing; everything else lists the internal packages it may import.
#: `services` is deliberately absent -- "nothing imports services" (root AGENTS.md).
ALLOWED: dict[str, frozenset[str]] = {
    "contracts": frozenset(),
    "platform": frozenset({"contracts"}),
    "gateway": frozenset({"contracts", "platform"}),
    "knowledge": frozenset({"contracts", "platform", "gateway"}),
    "memory": frozenset({"contracts", "platform", "gateway"}),
    "deliverables": frozenset({"contracts", "platform"}),
    "sovereignty": frozenset({"contracts", "platform"}),
    # memory joined tools with the memory manager (docs/adr/0009): an agent recalls what the
    # workbench remembers through the memory.recall tool, i.e. through the policy
    # chokepoint -- the same reason retrieval is reached through docs.* and never imported
    # by the runtime.
    "tools": frozenset({"contracts", "platform", "gateway", "knowledge", "deliverables", "memory"}),
    "runtime": frozenset({"contracts", "platform", "gateway", "tools", "memory"}),
}


def _imported_citadel_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root.startswith("citadel_") or root == "services":
                    roots.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                root = node.module.split(".")[0]
                if root.startswith("citadel_") or root == "services":
                    roots.add(root)
    return roots


def _offending_for_package(source: str, *, package: str) -> set[str]:
    """Every disallowed import root, given the file belongs to `package`."""
    allowed_pkgs = ALLOWED.get(package, frozenset())
    allowed_roots = {f"citadel_{p}" for p in allowed_pkgs} | {f"citadel_{package}"}
    found = _imported_citadel_roots(source)
    offending = set()
    for root in found:
        if root == "services":
            offending.add(root)  # nothing imports services, ever
        elif root not in allowed_roots:
            offending.add(root)
    return offending


def test_every_package_only_imports_what_the_dependency_rule_allows():
    offenders = []
    for path in all_source_files():
        package = package_of(path)
        if package is None or package not in ALLOWED:
            continue  # services/ may import anything; not a source of violations itself
        offending = _offending_for_package(path.read_text(encoding="utf-8"), package=package)
        if offending:
            offenders.append(f"{path} ({package}) imports {sorted(offending)}")

    assert offenders == [], "forbidden import(s) crossing a package boundary: " + "; ".join(
        offenders
    )


def test_the_detector_catches_runtime_importing_knowledge_and_deliverables():
    """The two edges root AGENTS.md names explicitly as tempting: retrieval and
    document generation are reached through docs.*/doc.* tools, not a direct import."""
    offending = _offending_for_package(
        control("runtime_imports_knowledge"), package="runtime"
    )
    assert offending == {"citadel_knowledge", "citadel_deliverables"}


def test_the_detector_is_not_vacuous():
    clean_runtime_source = (
        "from citadel_contracts.domain import Task\n"
        "from citadel_tools.registry import get_tool\n"
    )
    assert _offending_for_package(clean_runtime_source, package="runtime") == set()

    # A package importing exactly its own allowed set is not a violation.
    knowledge_source = "from citadel_gateway.routing import route\n"
    assert _offending_for_package(knowledge_source, package="knowledge") == set()

    # contracts permits nothing at all.
    assert _offending_for_package(
        "from citadel_platform.db import SessionLocal\n", package="contracts"
    ) == {"citadel_platform"}
