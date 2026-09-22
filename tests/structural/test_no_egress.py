"""No outbound network call outside the allowed clients.

This is defense in depth, not the enforcement mechanism -- the real boundary is
`ops/nftables/`'s default-deny ruleset on the WSL2 distro's own network namespace
(ADR-0005), which holds even if every line of Python here is wrong. What this detector
adds is a fast, offline signal: a package that has no business reaching the network
shouldn't even import something capable of it.

Two packages are allowed to import a networking library:
  - `gateway`: the declared inference clients (Ollama/vLLM) it talks to.
  - `sovereignty`: the "deliberate probe" (root AGENTS.md) that proves egress is
    actually blocked has to be able to attempt a connection on purpose.
Nothing else does -- `services/` included, since a service that needs the network talks
to the gateway, not to a socket directly.

Import-level check, not a call-level one: the presence of the import is already the
signal for a package that should have zero network capability. Documented blind spot:
a package could reach the network through a dependency's own transitive import without
importing the client library by name itself -- this catches the direct case, not a
laundered one.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control, package_of

_NETWORK_CLIENT_ROOTS = frozenset({"requests", "httpx", "aiohttp", "urllib3"})
_ALLOWED_PACKAGES = frozenset({"gateway", "sovereignty"})


def _imported_network_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _NETWORK_CLIENT_ROOTS:
                    roots.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                root = node.module.split(".")[0]
                if root in _NETWORK_CLIENT_ROOTS:
                    roots.add(root)
    return roots


def test_no_disallowed_package_imports_a_network_client():
    offenders = []
    for path in all_source_files():
        package = package_of(path)
        if package in _ALLOWED_PACKAGES:
            continue
        offending = _imported_network_roots(path.read_text(encoding="utf-8"))
        if offending:
            offenders.append(f"{path} ({package or 'services'}) imports {sorted(offending)}")

    assert offenders == [], "network client imported outside gateway/sovereignty: " + "; ".join(
        offenders
    )


def test_the_detector_catches_the_negative_control():
    offending = _imported_network_roots(control("raw_egress_call"))
    assert offending == {"requests", "httpx"}


def test_the_detector_is_not_vacuous():
    assert _imported_network_roots("import json\nimport os\n") == set()
