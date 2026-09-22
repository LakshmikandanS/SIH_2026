"""Ingest refuses a document above the active profile's `classification_ceiling`, and
never defaults a missing classification or ACL. `packages/knowledge/AGENTS.md`: "A
document without classification and ACL metadata is rejected, loudly. Never defaulted.
Defaulting a classification is how confidential material leaks, and a rejected upload
is a five-second fix while a mislabelled one is permanent." This is also the mechanical
form of ADR-0003's `classification_ceiling: public` on `hpc-eval` and invariant 11 in
root AGENTS.md ("confidential material never reaches hpc-eval") -- both depend on
ingest never quietly inventing a classification for a document that didn't declare one.

Forward-looking, like test_inference_isolation and test_single_chokepoint:
`citadel_knowledge`'s ingest pipeline is not yet written (M1 work). On a clean tree
this passes because there is nothing to scan; the negative control proves the pattern
match works today.

AST-based: flags `x.get("classification"/"acl", <default>)` (two-arg get with any
default) and `x.get("classification"/"acl") or <fallback>` (the same silent-default
idiom spelled with `or` instead of a second argument) -- both hand a document a
classification or ACL nobody declared.
"""

from __future__ import annotations

import ast

from conftest import control, source_files

_GUARDED_KEYS = frozenset({"classification", "acl"})


def _is_guarded_get_call(node: ast.AST) -> str | None:
    """The key being defaulted, if `node` is `x.get("classification"/"acl", ...)`.

    Takes `ast.AST`, not `ast.expr`: its callers pull `node` straight out of
    `ast.walk()`, which yields every node kind, not just expressions."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            key = first_arg.value
            if key in _GUARDED_KEYS:
                return key
    return None


def _silent_classification_defaults(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        # x.get("classification", default) -- a two-argument get supplies a default.
        key = _is_guarded_get_call(node)
        if key is not None and isinstance(node, ast.Call) and len(node.args) >= 2:
            hits.append(f'.get("{key}", <default>)')
        # x.get("classification") or fallback -- the same idiom, spelled with `or`.
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                inner_key = _is_guarded_get_call(value)
                if inner_key is not None:
                    hits.append(f'.get("{inner_key}") or <fallback>')
    return hits


def test_knowledge_ingest_never_defaults_classification_or_acl():
    offenders = []
    for path in source_files(package="knowledge"):
        hits = _silent_classification_defaults(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path}: {hits}")

    assert offenders == [], "ingest path defaults a missing classification/ACL: " + "; ".join(
        offenders
    )


def test_the_detector_catches_the_negative_control():
    hits = _silent_classification_defaults(control("missing_classification_defaulted"))
    assert hits == ['.get("classification", <default>)', '.get("acl") or <fallback>']


def test_the_detector_is_not_vacuous():
    # Rejecting loudly on a missing key -- no default anywhere -- must not be flagged.
    clean = (
        "def normalise_upload(raw):\n"
        "    if 'classification' not in raw or 'acl' not in raw:\n"
        "        raise ValueError('document missing classification/ACL metadata')\n"
        "    return {'classification': raw['classification'], 'acl': raw['acl']}\n"
    )
    assert _silent_classification_defaults(clean) == []
    # A default on an unrelated key is not this detector's concern.
    assert _silent_classification_defaults('title = raw.get("title", "untitled")\n') == []
