"""A small, safe expression language for join conditions.

``steps.check.ok and not steps.backup.ok``, ``steps['b-2'].exit_code == 0``,
``run.missed``, ``vars.mode == "full"``. Parsed with :mod:`ast` and walked by
hand, never ``eval``'d, with the same restrictions as placement expressions
(rook/core/facts.py): boolean operators, ``not``, comparisons (including
``in``), constants, lists/tuples, and lookups rooted at ``steps``, ``run``,
``vars`` or ``job`` by attribute or constant subscript. No calls, no
arithmetic, no other names. A missing key reads as ``None``.
"""
from __future__ import annotations

import ast
from typing import Any

ROOTS = ("steps", "run", "vars", "job")
_CMP = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a is not None and b is not None and a < b,
    ast.LtE: lambda a, b: a is not None and b is not None and a <= b,
    ast.Gt: lambda a, b: a is not None and b is not None and a > b,
    ast.GtE: lambda a, b: a is not None and b is not None and a >= b,
    ast.In: lambda a, b: b is not None and a in b,
    ast.NotIn: lambda a, b: b is not None and a not in b,
}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
            ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List, ast.Attribute, ast.Subscript,
            *_CMP)
MAX_LEN = 500


def compile_expr(text: str) -> ast.Expression:
    """Parse and check an expression; raises ValueError saying what is wrong."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("expression is empty")
    if len(text) > MAX_LEN:
        raise ValueError(f"expression is longer than {MAX_LEN} characters")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"expression syntax error: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"expression may not use {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ROOTS:
            raise ValueError(f"unknown name {node.id!r}; expressions start at {', '.join(ROOTS)}")
        if isinstance(node, ast.Subscript) and not (
                isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, (str, int))):
            raise ValueError("subscripts must be a constant string or number")
    return tree


def step_refs(tree: ast.AST) -> set[str]:
    """Step ids an expression reads (``steps.<id>`` / ``steps['<id>']``)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.value, ast.Name) \
                and node.value.id == "steps":
            key = node.attr if isinstance(node, ast.Attribute) else node.slice.value
            out.add(str(key))
    return out


def _lookup(obj: Any, key: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    if isinstance(obj, (list, tuple)) and isinstance(key, int) and -len(obj) <= key < len(obj):
        return obj[key]
    return None


def _eval(node: ast.AST, scope: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, scope)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval(e, scope) for e in node.elts]
    if isinstance(node, ast.Name):
        return scope.get(node.id)
    if isinstance(node, ast.Attribute):
        return _lookup(_eval(node.value, scope), node.attr)
    if isinstance(node, ast.Subscript):
        return _lookup(_eval(node.value, scope), node.slice.value)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(v, scope) for v in node.values)
        return any(_eval(v, scope) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return not _eval(node.operand, scope)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, scope)
            try:
                if not _CMP[type(op)](left, right):
                    return False
            except TypeError:
                return False
            left = right
        return True
    raise ValueError(f"cannot evaluate {type(node).__name__}")


def evaluate(text_or_tree: "str | ast.Expression", scope: dict) -> bool:
    tree = compile_expr(text_or_tree) if isinstance(text_or_tree, str) else text_or_tree
    return bool(_eval(tree, scope))
