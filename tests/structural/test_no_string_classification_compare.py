"""Classification is compared exactly one way: `Classification.rank()` /
`Classification.exceeds()`. Never a raw `<`/`>`/`<=`/`>=` on the classification strings
themselves -- `"PUBLIC" > "INTERNAL"` is true lexicographically and meaningless as a
lattice comparison, which is precisely the bug `citadel_contracts.classification`'s own
test suite exists to prevent one layer down. This detector catches the mistake at every
other call site that might reach for a bare operator instead of the lattice.

AST-based: flags a Compare node using Gt/Lt/GtE/LtE where either side is an attribute
access whose name ends in "classification" (so `doc.classification` and
`user.max_classification` are both caught). Does not flag `Classification.rank(x) >
Classification.rank(y)` -- both sides there are Call nodes, not bare attribute access,
which is exactly the point: that is the correct pattern, not the violation.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control

_COMPARE_OPS = (ast.Gt, ast.Lt, ast.GtE, ast.LtE)


def _is_classification_attr(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr.lower().endswith("classification")


def _string_classification_compares(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for op, right in zip(node.ops, node.comparators):
            if not isinstance(op, _COMPARE_OPS):
                continue
            left = node.left if right is node.comparators[0] else operands[operands.index(right) - 1]
            if _is_classification_attr(left) or _is_classification_attr(right):
                hits.append(ast.dump(node))
    return hits


def test_classification_is_never_compared_as_a_raw_string():
    offenders = []
    for path in all_source_files():
        hits = _string_classification_compares(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path}: {len(hits)} raw comparison(s)")

    assert offenders == [], "classification compared with a raw operator, not the lattice: " + "; ".join(
        offenders
    )


def test_the_detector_catches_the_negative_control():
    hits = _string_classification_compares(control("string_classification_compare"))
    assert len(hits) == 2


def test_the_detector_is_not_vacuous():
    # The correct pattern -- comparing through rank() -- must not be flagged.
    clean = (
        "from citadel_contracts.classification import Classification\n"
        "def is_denied(doc, user):\n"
        "    return Classification.rank(doc.classification) > Classification.rank(user.max_classification)\n"
    )
    assert _string_classification_compares(clean) == []
    # An unrelated comparison must not be flagged either.
    assert _string_classification_compares("if a.count > b.count:\n    pass\n") == []
