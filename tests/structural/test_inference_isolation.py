"""Only `gateway` imports an inference client. Everything else reaches a model through
the gateway's routing, never by talking to Ollama/vLLM/a hosted API directly -- that is
what makes the routing table, the residency admission semaphores (ADR-0004), and
"degrade honestly" (root AGENTS.md) actually load-bearing rather than one path among
several.

AST-based import check. `gateway` is the only package this detector exempts;
`services/` is exempt too (root AGENTS.md: "services may import anything") -- an entry
point wiring the gateway in is not the same thing as bypassing it.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control, package_of

#: Known inference-client module roots. Same blind spot as test_no_hardcoded_models:
#: a brand-new client library needs adding here deliberately.
_INFERENCE_CLIENT_ROOTS = frozenset({"ollama", "openai", "anthropic", "vllm", "llama_cpp"})

_EXEMPT_PACKAGES = frozenset({"gateway"})


def _imported_inference_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _INFERENCE_CLIENT_ROOTS:
                    roots.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                root = node.module.split(".")[0]
                if root in _INFERENCE_CLIENT_ROOTS:
                    roots.add(root)
    return roots


def test_no_package_but_gateway_imports_an_inference_client():
    offenders = []
    for path in all_source_files():
        package = package_of(path)
        if package in _EXEMPT_PACKAGES:
            continue
        offending = _imported_inference_roots(path.read_text(encoding="utf-8"))
        if offending:
            offenders.append(f"{path} ({package or 'services'}) imports {sorted(offending)}")

    assert offenders == [], "inference client imported outside gateway: " + "; ".join(offenders)


def test_the_detector_catches_the_negative_control():
    offending = _imported_inference_roots(control("inference_import_in_runtime"))
    assert offending == {"ollama", "openai"}


def test_the_detector_is_not_vacuous():
    assert _imported_inference_roots("import json\nfrom citadel_contracts import Task\n") == set()
