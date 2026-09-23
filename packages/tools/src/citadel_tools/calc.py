"""calc.evaluate: arithmetic with retained, displayable working.

The working is part of the result, not a log line (registry/tools.yaml). Every step is
recorded as it is reduced -- `9.2 - 8.4 = 0.8`, then `0.8 / 0.25 = 3.2` -- and the
whole computation becomes citable evidence (C1, C2...), so a derived number in a
deliverable traces to recorded working instead of to a model's mental arithmetic.

The evaluator walks a Python AST and accepts only numbers, the four operations, powers,
modulo, parentheses, a short list of functions and the constants pi and e -- never
names, attributes, calls to anything else, or comprehensions.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Callable

from citadel_contracts.domain import Resource
from citadel_knowledge import register_computation

from citadel_tools.context import Invocation, ToolContext, ToolFailure, ToolOutput
from citadel_tools.plugins import ToolPlugin, digest_of, task_resource

MAX_LENGTH = 400
_LABEL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]{0,60})\s*=(?!=)\s*(.+)$", re.DOTALL)

_BINARY: dict[type, tuple[str, Callable[[float, float], float]]] = {
    ast.Add: ("+", operator.add),
    ast.Sub: ("-", operator.sub),
    ast.Mult: ("*", operator.mul),
    ast.Div: ("/", operator.truediv),
    ast.FloorDiv: ("//", operator.floordiv),
    ast.Mod: ("%", operator.mod),
    ast.Pow: ("^", operator.pow),
}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
    "min": min, "max": max, "round": round, "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}


def fmt(value: float) -> str:
    if isinstance(value, bool):
        raise ToolFailure("booleans are not numbers here")
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return format(float(value), ".10g")


class _Evaluator:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def visit(self, node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = self.visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = self.visit(node.left)
            right = self.visit(node.right)
            symbol, fn = _BINARY[type(node.op)]
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e12):
                raise ToolFailure("exponent or base too large")
            try:
                value = float(fn(left, right))
            except ZeroDivisionError:
                raise ToolFailure(f"division by zero in {fmt(left)} {symbol} {fmt(right)}") from None
            self._check(value)
            self.steps.append(f"{fmt(left)} {symbol} {fmt(right)} = {fmt(value)}")
            return value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
            args = [self.visit(a) for a in node.args]
            try:
                value = float(_FUNCTIONS[node.func.id](*args))
            except (ValueError, TypeError) as exc:
                raise ToolFailure(f"{node.func.id}: {exc}") from None
            self._check(value)
            self.steps.append(f"{node.func.id}({', '.join(fmt(a) for a in args)}) = {fmt(value)}")
            return value
        raise ToolFailure(f"not allowed in an expression: {ast.dump(node)[:80]}")

    @staticmethod
    def _check(value: float) -> None:
        if math.isnan(value) or math.isinf(value):
            raise ToolFailure("the result is not a finite number")


def evaluate_expression(expression: str) -> tuple[str, float, list[str]]:
    """(label, value, working) -- `label` is the optional `name =` prefix."""
    text = expression.strip()
    if not text or len(text) > MAX_LENGTH:
        raise ToolFailure(f"expression must be 1-{MAX_LENGTH} characters")
    label = ""
    match = _LABEL.match(text)
    if match:
        label, text = match.group(1).strip(), match.group(2).strip()
    normalised = text.replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
    try:
        tree = ast.parse(normalised, mode="eval")
    except SyntaxError as exc:
        raise ToolFailure(f"cannot parse {text!r}: {exc.msg}") from None
    evaluator = _Evaluator()
    value = evaluator.visit(tree)
    return label, value, evaluator.steps or [f"{fmt(value)}"]


def _resource(ctx: ToolContext, args: dict[str, Any]) -> Resource:
    return task_resource(ctx, "computation", digest_of(str(args["expression"])))


def _run(ctx: ToolContext, args: dict[str, Any], invocation: Invocation) -> ToolOutput:
    expression = str(args["expression"])
    label, value, working = evaluate_expression(expression)
    shown = fmt(value)
    stated = expression.split("=", 1)[1].strip() if label else expression.strip()
    text = f"{label + ': ' if label else ''}{stated} = {shown}"
    evidence_id = register_computation(
        ctx.db,
        ctx.task_id,
        text=text,
        classification=ctx.task_classification,
        detail={"expression": stated, "label": label, "result": shown, "working": working, "tool": "calc.evaluate"},
    )
    return ToolOutput(
        data={
            "evidence_id": evidence_id,
            "expression": stated,
            "result": shown,
            "working": working,
            "how_to_cite": f"Cite this value as [{evidence_id}].",
        },
        summary=f"{text}  [{evidence_id}]",
        evidence=[evidence_id],
    )


PLUGINS = {"calc.evaluate": ToolPlugin("calc.evaluate", _resource, _run)}

__all__ = ["PLUGINS", "evaluate_expression", "fmt"]
