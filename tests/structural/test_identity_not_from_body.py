"""Identity never comes from a request body. A handler's caller-supplied payload is
never trusted for who the caller is -- user_id, role, clearance and department come
from the verified token the identity middleware attaches, never from a field an agent
or a curl command could simply set. Reading identity from the body is how the ACL
demonstration (ADR-0001 Q7) would stop meaning anything.

AST-based pattern match, not a full data-flow analysis -- documented blind spot: this
catches `body["user_id"]` and `body.get("user_id")` on a small set of body-shaped
variable names, not identity read from an arbitrarily-renamed or destructured variable
several assignments away.
"""

from __future__ import annotations

import ast

from conftest import all_source_files, control

_BODY_NAMES = frozenset({"body", "payload", "data", "req_body", "request_body", "json_body"})
_IDENTITY_KEYS = frozenset({"user_id", "role", "roles", "clearance", "department", "username"})


def _identity_reads_from_body(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        # body["user_id"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in _BODY_NAMES
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value in _IDENTITY_KEYS
        ):
            key = node.slice.value
            hits.append(f'{node.value.id}["{key}"]')
        # body.get("user_id", ...)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _BODY_NAMES
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in _IDENTITY_KEYS
        ):
            key = node.args[0].value
            hits.append(f'{node.func.value.id}.get("{key}")')
    return hits


def test_no_handler_reads_identity_from_the_request_body():
    offenders = []
    for path in all_source_files():
        hits = _identity_reads_from_body(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path}: {hits}")

    assert offenders == [], "identity read from a request body: " + "; ".join(offenders)


def test_the_detector_catches_the_negative_control():
    hits = _identity_reads_from_body(control("identity_from_body"))
    assert hits == ['body["user_id"]', 'body.get("department")']


def test_the_detector_is_not_vacuous():
    assert _identity_reads_from_body('name = body["display_name"]\n') == []
    assert _identity_reads_from_body('user_id = token.claims["user_id"]\n') == []
