"""No model id, VRAM figure, or parameter count lives outside `registry/`.

Invariant 1 (root AGENTS.md): models are looked up by logical id through the gateway's
registry, never named or sized in code. `registry/models.*.yaml` is the only place a
concrete tag like "qwen3:4b" or a VRAM estimate belongs.

Grep/AST hybrid, not a full type-aware check -- documented blind spots:
  - the model-family keyword list is finite and known-model-specific; a brand-new model
    family added to the registry later needs a keyword added here too, deliberately,
    the same way a new event type needs registering (this is the cost of an open
    registry: the *shape* the registry can hold is open, but a detector that greps for
    known names has to be told about a name it has never seen).
  - the VRAM/param-count check keys on identifier names containing "vram" or
    "param_count"; a numeric literal hidden behind a differently-named constant is not
    caught by this pass.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control

#: Substrings that identify a concrete model family/tag. Sourced from the model
#: families actually named in registry/models.*.yaml -- grounded in real data, not
#: guessed. A model name is only ever allowed to appear in registry/ (data), never in
#: source (code): that boundary is the whole point of the registry-as-data pattern.
_MODEL_KEYWORDS = (
    "qwen", "granite", "llama", "phi-", "gemma", "mistral",
    "nomic-embed", "bge-", "deepseek",
)

_SUSPICIOUS_NAME_FRAGMENTS = ("vram", "param_count")


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _suspicious_numeric_assignments(tree: ast.AST) -> list[str]:
    """Names of assignment targets that look like a VRAM figure or a parameter count
    and are bound to a numeric literal."""
    hits = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, (int, float))):
            continue
        for target in targets:
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name and any(frag in name.lower() for frag in _SUSPICIOUS_NAME_FRAGMENTS):
                hits.append(name)
    return hits


def _findings(source: str) -> list[str]:
    tree = ast.parse(source)
    findings = []
    for value in _string_constants(tree):
        lowered = value.lower()
        for keyword in _MODEL_KEYWORDS:
            if keyword in lowered:
                findings.append(f"model-family string literal {value!r}")
                break
    findings.extend(f"suspicious numeric constant {name!r}" for name in _suspicious_numeric_assignments(tree))
    return findings


def test_no_model_names_or_capacity_figures_in_source():
    offenders = []
    for path in all_source_files():
        findings = _findings(path.read_text(encoding="utf-8"))
        if findings:
            offenders.append(f"{path}: {findings}")

    assert offenders == [], "hardcoded model identity/capacity outside registry/: " + "; ".join(
        offenders
    )


def test_the_detector_catches_the_negative_control():
    findings = _findings(control("hardcoded_model"))
    assert len(findings) >= 3, f"expected at least 3 findings (2 model tags + 2 numeric), got {findings}"
    assert any("qwen" in f.lower() for f in findings)
    assert any("granite" in f.lower() for f in findings)
    assert any("vram" in f.lower() for f in findings)
    assert any("param_count" in f.lower() for f in findings)


def test_the_detector_is_not_vacuous():
    assert _findings("x = 1\ny = 'hello world'\n") == []
    # A registry *path* string is not a model identity and must not be flagged.
    assert _findings('CONFIG_PATH = "registry/models.demo-local.yaml"\n') == []
