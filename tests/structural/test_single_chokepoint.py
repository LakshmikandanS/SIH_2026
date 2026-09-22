"""No tool dispatch outside the chokepoint. `citadel_tools` is where a capability check,
a policy decision, and a signed receipt happen before a tool runs (root AGENTS.md's
two-check authorization model); a direct call to a tool implementation or the tool
registry from anywhere else -- however convenient in the moment -- skips all three.

This detector is necessarily forward-looking: the chokepoint itself is M0 task-20 work,
not yet written. What it fixes now is the naming convention that later work must follow
(a public `TOOL_REGISTRY` and an `execute_tool`/`dispatch_tool` entry point are the
shapes this detector knows to flag outside citadel_tools) -- so the moment task 20 adds
real code, a bypass is caught immediately rather than "eventually, once someone
remembers to write this test."

Pattern-based (AST for the call/subscript shapes, since a full call-graph analysis is
out of scope): documented blind spot is a bypass that goes through neither of these two
named shapes.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control, package_of

_EXEMPT_PACKAGES = frozenset({"tools"})
_DISPATCH_FUNCTION_NAMES = frozenset({"execute_tool", "dispatch_tool"})
_REGISTRY_NAMES = frozenset({"TOOL_REGISTRY"})
_BYPASS_IMPORT_MODULES = frozenset({"citadel_tools.registry", "citadel_tools.dispatch"})


def _chokepoint_bypasses(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id in _REGISTRY_NAMES:
                hits.append(f"{node.value.id}[...]")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _DISPATCH_FUNCTION_NAMES:
                hits.append(f"{name}(...)")
        elif isinstance(node, ast.ImportFrom) and node.module in _BYPASS_IMPORT_MODULES:
            hits.append(f"from {node.module} import ...")
    return hits


def test_no_direct_tool_dispatch_outside_the_chokepoint():
    offenders = []
    for path in all_source_files():
        if package_of(path) in _EXEMPT_PACKAGES:
            continue
        hits = _chokepoint_bypasses(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path}: {hits}")

    assert offenders == [], "tool dispatched outside citadel_tools' chokepoint: " + "; ".join(
        offenders
    )


def test_the_detector_catches_the_negative_control():
    hits = _chokepoint_bypasses(control("direct_tool_call_bypassing_chokepoint"))
    # two bypass imports (registry, dispatch), the TOOL_REGISTRY[...] subscript, and
    # the execute_tool(...) call.
    assert len(hits) == 4


def test_the_detector_is_not_vacuous():
    assert _chokepoint_bypasses("x = some_dict['key']\n") == []
    assert _chokepoint_bypasses("result = some_other_function(1, 2)\n") == []
