"""The expression language models are written in.

A small subset of Python syntax, parsed with `ast` and checked against a whitelist. Nothing is ever
executed by Python: the compiler turns the tree into SQL, the interpreter walks it with exact
fractions, and the prover turns it into Z3 terms. `alias.field` is the only attribute form allowed;
it names a field of a joined entity and is resolved through the ontology, never with getattr.
"""
from __future__ import annotations

import ast
from typing import Set, Tuple


class ExprError(ValueError):
    """An expression is malformed or refers to something unknown."""


_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Attribute,
)


class Expr:
    """A parsed expression: `.body` is the ast node, `.src` the normalised text."""

    __slots__ = ("body", "src")

    def __init__(self, body: ast.AST, src: str):
        self.body = body
        self.src = src

    def __repr__(self):
        return f"Expr({self.src!r})"


def parse(src) -> Expr:
    if isinstance(src, Expr):
        return src
    if isinstance(src, bool):
        src = str(src)
    elif isinstance(src, (int, float)):
        src = repr(src)
    if not isinstance(src, str) or not src.strip():
        raise ExprError(f"empty or non-text expression: {src!r}")
    text = " ".join(src.strip().split())
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in `{text}`: {e.msg}")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"`{type(node).__name__}` is not allowed in expressions (in `{text}`)")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExprError(f"only plain function calls are allowed (in `{text}`)")
            if any(isinstance(a, ast.Starred) for a in node.args):
                raise ExprError(f"starred arguments are not allowed (in `{text}`)")
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                raise ExprError(f"only `alias.field` is allowed, not nested attributes (in `{text}`)")
            if node.attr.startswith("_") or node.value.id.startswith("_"):
                raise ExprError(f"names may not start with '_' (in `{text}`)")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ExprError(f"names may not start with '_' (in `{text}`)")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            raise ExprError(f"unsupported literal {node.value!r} (in `{text}`)")
    return Expr(tree.body, text)


def ref_name(node) -> str:
    """'field' for a Name, 'alias.field' for an Attribute."""
    if isinstance(node, ast.Attribute):
        return f"{node.value.id}.{node.attr}"
    return node.id


def refs(expr: Expr) -> Tuple[Set[str], Set[str]]:
    """(names, function calls) used in an expression; `alias.field` is reported as one dotted name."""
    calls, func_nodes, inner = set(), set(), set()
    for n in ast.walk(expr.body):
        if isinstance(n, ast.Call):
            calls.add(n.func.id)
            func_nodes.add(id(n.func))
        if isinstance(n, ast.Attribute):
            inner.add(id(n.value))
    names = set()
    for n in ast.walk(expr.body):
        if isinstance(n, ast.Attribute):
            names.add(ref_name(n))
        elif isinstance(n, ast.Name) and id(n) not in func_nodes and id(n) not in inner:
            names.add(n.id)
    return names, calls


def aggregate_nodes(expr: Expr, aggregates) -> list:
    return [n for n in ast.walk(expr.body) if isinstance(n, ast.Call) and n.func.id in aggregates]
