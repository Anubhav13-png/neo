"""Per-row conditions as Z3 formulas over one symbolic row, for the fidelity check.

A model's `rows` filter and a requirement's filter are both conditions on one row. Here every field becomes a Z3
constant (numbers and dates are Real, booleans Bool), so Z3 can decide whether one condition implies the other for
every possible row, not only the rows in this snapshot. Ontology fields defined by an expression and the model's
own defines are expanded first, so `is_assisted_failed` and `deal_category == 'Assisted / failed-bank deal'` become
the same formula.

Anything Z3 cannot reason about exactly (a text comparison, a function of text, `//`) becomes an opaque
true/false atom named by its normalised text. That only weakens what can be proved: an implication that holds with
the atoms free holds for every real row, so the proofs stay sound. Missing values are not modelled; the lake
counts in fidelity.py cover the real rows, missing values included.
"""
from __future__ import annotations

import ast
import datetime as dt
from fractions import Fraction
from typing import Dict, Optional

import z3

from ..dsl.parse import Expr, parse, ref_name


class Unsupported(ValueError):
    pass


def _date_number(text: str) -> Optional[int]:
    try:
        return dt.date.fromisoformat(str(text)[:10]).toordinal()
    except ValueError:
        return None


class RowLogic:
    def __init__(self, onto, entity: str, defines: Optional[Dict[str, Expr]] = None):
        self.onto, self.entity = onto, entity
        self.defines = defines or {}
        self.consts: Dict[str, object] = {}
        self._expanding: set = set()

    # ------------------------------------------------------------------ names
    def _field(self, name: str):
        if "." in name:
            return None
        return self.onto.field(self.entity, name)

    def _const(self, name: str, sort):
        if name not in self.consts:
            self.consts[name] = {"Real": z3.Real, "Bool": z3.Bool}[sort](name)
        return self.consts[name]

    def atom(self, text: str):
        return self._const(" ".join(text.split()), "Bool")

    def _name(self, name: str):
        """(kind, z3 term): kind is num | bool | text | date."""
        if name in self.defines:
            return self._expand(name, self.defines[name])
        f = self._field(name)
        if f is None:
            raise Unsupported(f"`{name}` is not a field of {self.entity}")
        if f.expression:
            return self._expand(name, parse(f.expression))
        kind = f.dtype or "number"
        if kind == "bool":
            return "bool", self._const(name, "Bool")
        if kind == "text":
            return "text", name
        if kind == "date":
            return "date", self._const(name, "Real")
        return "num", self._const(name, "Real")

    def _expand(self, name: str, e: Expr):
        if name in self._expanding:
            raise Unsupported(f"`{name}` refers to itself")
        self._expanding.add(name)
        try:
            return self.term(e.body)
        finally:
            self._expanding.discard(name)

    # ------------------------------------------------------------------ formulas
    def formula(self, e) -> object:
        """A per-row condition (Expr or text) as a Z3 Bool."""
        e = parse(e)
        kind, t = self.term(e.body)
        if kind != "bool":
            raise Unsupported(f"`{e.src}` is not a condition")
        return t

    def term(self, n):
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool):
                return "bool", z3.BoolVal(v)
            if isinstance(v, (int, float)):
                f = Fraction(repr(v)) if isinstance(v, float) else Fraction(v)
                return "num", z3.RealVal(f)
            if isinstance(v, str):
                return "str", v
            raise Unsupported("missing value literal")
        if isinstance(n, (ast.Name, ast.Attribute)):
            name = ref_name(n) if isinstance(n, ast.Attribute) else n.id
            if name in ("True", "False"):
                return "bool", z3.BoolVal(name == "True")
            return self._name(name)
        if isinstance(n, ast.BoolOp):
            parts = [self._bool(v) for v in n.values]
            return "bool", (z3.And(*parts) if isinstance(n.op, ast.And) else z3.Or(*parts))
        if isinstance(n, ast.UnaryOp):
            if isinstance(n.op, ast.Not):
                return "bool", z3.Not(self._bool(n.operand))
            k, t = self.term(n.operand)
            if k != "num":
                raise Unsupported("sign of a non-number")
            return "num", (-t if isinstance(n.op, ast.USub) else t)
        if isinstance(n, ast.Compare):
            return "bool", self._compare(n)
        if isinstance(n, ast.BinOp):
            return "num", self._arith(n)
        if isinstance(n, ast.Call):
            return self._call(n)
        if isinstance(n, ast.IfExp):
            c = self._bool(n.test)
            (ka, a), (kb, b) = self.term(n.body), self.term(n.orelse)
            if ka != kb or ka not in ("num", "bool"):
                raise Unsupported("if-else over text")
            return ka, z3.If(c, a, b)
        raise Unsupported(f"`{ast.unparse(n)}`")

    def _bool(self, n):
        try:
            k, t = self.term(n)
        except Unsupported:
            return self.atom(ast.unparse(n))
        if k != "bool":
            raise Unsupported(f"`{ast.unparse(n)}` is not a condition")
        return t

    def _arith(self, n: ast.BinOp):
        (ka, a), (kb, b) = self.term(n.left), self.term(n.right)
        if ka != "num" or kb != "num":
            raise Unsupported("arithmetic on a non-number")
        if isinstance(n.op, ast.Add):
            return a + b
        if isinstance(n.op, ast.Sub):
            return a - b
        if isinstance(n.op, ast.Mult):
            return a * b
        if isinstance(n.op, ast.Div):
            d = z3.simplify(b)
            if not z3.is_rational_value(d) or d.numerator_as_long() == 0:
                raise Unsupported("division by a field")
            return a / b
        raise Unsupported(f"operator in `{ast.unparse(n)}`")

    def _compare(self, n: ast.Compare):
        parts, left = [], n.left
        for op, right in zip(n.ops, n.comparators):
            try:
                parts.append(self._compare_pair(op, left, right))
            except Unsupported:
                parts.append(self.atom(ast.unparse(ast.Compare(left=left, ops=[op], comparators=[right]))))
            left = right
        return parts[0] if len(parts) == 1 else z3.And(*parts)

    def _text_eq(self, field: str, value: str):
        return self.atom(f"{field} == {value!r}")

    def _compare_pair(self, op, ln, rn):
        if isinstance(op, (ast.In, ast.NotIn)):
            if not isinstance(rn, (ast.List, ast.Tuple)):
                raise Unsupported("`in` needs a list")
            k, t = self.term(ln)
            opts = []
            for el in rn.elts:
                ke, te = self.term(el)
                if k == "text" and ke == "str":
                    opts.append(self._text_eq(t, te))
                elif k == "num" and ke == "num":
                    opts.append(t == te)
                else:
                    raise Unsupported("mixed `in` list")
            f = z3.Or(*opts) if opts else z3.BoolVal(False)
            return z3.Not(f) if isinstance(op, ast.NotIn) else f
        (ka, a), (kb, b) = self.term(ln), self.term(rn)
        if ka == "str" and kb == "text":            # 'TX' == target_state
            ka, a, kb, b = kb, b, ka, a
            op = {ast.Lt: ast.Gt(), ast.Gt: ast.Lt(), ast.LtE: ast.GtE(), ast.GtE: ast.LtE()}.get(type(op), op)
        if ka == "text" and kb == "str":
            if isinstance(op, ast.Eq):
                return self._text_eq(a, b)
            if isinstance(op, ast.NotEq):
                return z3.Not(self._text_eq(a, b))
            raise Unsupported("ordering of text")
        if ka == "date" and kb == "str":
            d = _date_number(b)
            if d is None:
                raise Unsupported("not a date")
            kb, b = "num", z3.RealVal(d)
            ka = "num"
        if kb == "date" and ka == "str":
            d = _date_number(a)
            if d is None:
                raise Unsupported("not a date")
            ka, a, kb = "num", z3.RealVal(d), "num"
        if ka == "date" and kb == "date":
            ka = kb = "num"
        if ka == "bool" and kb == "bool" and isinstance(op, (ast.Eq, ast.NotEq)):
            return (a == b) if isinstance(op, ast.Eq) else z3.Not(a == b)
        if ka != "num" or kb != "num":
            raise Unsupported("comparison of different kinds")
        return {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.Lt: lambda: a < b, ast.LtE: lambda: a <= b,
                ast.Gt: lambda: a > b, ast.GtE: lambda: a >= b}[type(op)]()

    def _call(self, n: ast.Call):
        fn = n.func.id
        if fn in ("not_null", "is_null") and len(n.args) == 1:
            a = self.atom(f"not_null({ast.unparse(n.args[0])})")
            return "bool", (a if fn == "not_null" else z3.Not(a))
        if fn == "implies" and len(n.args) == 2:
            return "bool", z3.Implies(self._bool(n.args[0]), self._bool(n.args[1]))
        if fn == "ite" and len(n.args) == 3:
            return self.term(ast.IfExp(test=n.args[0], body=n.args[1], orelse=n.args[2]))
        if fn in ("abs", "min", "max"):
            args = [self.term(a) for a in n.args]
            if any(k != "num" for k, _ in args):
                raise Unsupported(f"{fn} of a non-number")
            if fn == "abs":
                x = args[0][1]
                return "num", z3.If(x >= 0, x, -x)
            a, b = args[0][1], args[1][1]
            return "num", (z3.If(a <= b, a, b) if fn == "min" else z3.If(a >= b, a, b))
        raise Unsupported(f"function `{fn}`")


def implies(a, b, timeout_ms: int = 5000) -> Optional[bool]:
    """True when a → b holds for every row; False when Z3 finds a row where it fails; None when unknown."""
    s = z3.Solver()
    s.set("timeout", timeout_ms)
    s.add(a, z3.Not(b))
    r = s.check()
    return True if r == z3.unsat else (False if r == z3.sat else None)
