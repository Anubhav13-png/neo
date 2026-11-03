"""Model expressions as Z3 terms.

Constants (parameters, aggregate results computed by the lake, a row's field values) enter as exact
rationals; variables are Z3 unknowns. With `fold=False` arithmetic over constants is left to Z3, so the
uniqueness proof of a value is Z3 deriving it, not Python. A missing value inside a condition makes that
condition false, matching SQL filter semantics.
"""
from __future__ import annotations

import ast
from fractions import Fraction
from typing import Callable, Dict, List, Optional

import z3

from ..dsl import AGGREGATES
from ..dsl.parse import ref_name
from ..semantic.model import Scope


class Z3Error(ValueError):
    pass


def is_z3(v) -> bool:
    return isinstance(v, z3.ExprRef)


def z3val(v):
    if is_z3(v):
        return v
    if isinstance(v, bool):
        return z3.BoolVal(v)
    if isinstance(v, (int, Fraction)):
        f = Fraction(v)
        return z3.IntVal(f.numerator) if f.denominator == 1 else z3.RealVal(f"{f.numerator}/{f.denominator}")
    raise Z3Error(f"cannot use {v!r} in a solver expression")


def real(t):
    t = z3val(t)
    return z3.ToReal(t) if t.is_int() else t


def from_z3(v):
    v = z3.simplify(v)
    if z3.is_true(v):
        return True
    if z3.is_false(v):
        return False
    if z3.is_int_value(v):
        return Fraction(v.as_long())
    if z3.is_rational_value(v):
        return Fraction(v.numerator_as_long(), v.denominator_as_long())
    if z3.is_algebraic_value(v):
        return Fraction(v.approx(30).as_decimal(30).rstrip("?"))
    return None


def as_bool(v):
    """A condition as a Z3 bool; a missing value (None) is not true."""
    if v is None:
        return z3.BoolVal(False)
    return z3val(v) if not isinstance(v, bool) else z3.BoolVal(v)


class Z3Translator:
    """term(node, row) -> Python value or Z3 term.

    row        a function (alias, field) -> value for one row, or None for whole-set expressions
    rows       list of such functions, to build a symbolic aggregate over a finite set of rows
    agg_values node id -> value of aggregates already computed by the lake
    """

    def __init__(self, scope: Scope, defines: Dict[str, object], params: Dict[str, object],
                 zvars: Dict[str, object], agg_values: Optional[Dict[int, object]] = None, fold: bool = True,
                 field_eval: Optional[Callable] = None):
        self.scope = scope
        self.defines = defines
        self.params = params
        self.zvars = zvars
        self.agg_values = agg_values or {}
        self.fold = fold
        self.field_eval = field_eval      # (row, alias, Field) -> value

    def const(self, v):
        if self.fold or v is None or isinstance(v, (bool, str)):
            return v
        return z3val(v)

    def term(self, n, row=None, rows: Optional[List] = None):
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return Fraction(repr(v)) if isinstance(v, float) else Fraction(v)
            return v
        if isinstance(n, (ast.Name, ast.Attribute)):
            nm = ref_name(n)
            kind = self.scope.kind(nm)
            if kind == "param":
                return self.const(self.params[nm])
            if kind == "var":
                return self.zvars[nm]
            if kind == "define":
                if row is None:
                    raise Z3Error(f"`{nm}` is per-row")
                return self.term(self.defines[nm].body, row, rows)
            if kind == "field":
                if row is None:
                    raise Z3Error(f"`{nm}` is per-row")
                alias, f = self.scope.field(nm)
                return self.field_eval(row, alias, f)
            raise Z3Error(f"unknown name `{nm}`")
        if isinstance(n, ast.UnaryOp):
            v = self.term(n.operand, row, rows)
            if isinstance(n.op, ast.Not):
                if v is None:
                    return None
                return z3.Not(as_bool(v)) if is_z3(v) else (not v)
            if v is None:
                return None
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BoolOp):
            vals = [self.term(x, row, rows) for x in n.values]
            is_and = isinstance(n.op, ast.And)
            if any(is_z3(v) for v in vals):
                parts = [as_bool(v) for v in vals]
                return z3.And(*parts) if is_and else z3.Or(*parts)
            if is_and:
                return False if any(v is False for v in vals) else (None if any(v is None for v in vals) else True)
            return True if any(v is True for v in vals) else (None if any(v is None for v in vals) else False)
        if isinstance(n, ast.BinOp):
            a, b = self.term(n.left, row, rows), self.term(n.right, row, rows)
            if a is None or b is None:
                return None
            sym = is_z3(a) or is_z3(b)
            if isinstance(n.op, ast.Add):
                return (real(a) + real(b)) if sym else a + b
            if isinstance(n.op, ast.Sub):
                return (real(a) - real(b)) if sym else a - b
            if isinstance(n.op, ast.Mult):
                return (real(a) * real(b)) if sym else a * b
            if isinstance(n.op, ast.Div):
                if sym:
                    return real(a) / real(b)
                return None if b == 0 else Fraction(a) / Fraction(b)
            if sym:
                raise Z3Error("`//` and `%` cannot use solver variables")
            import math
            q = Fraction(math.floor(Fraction(a) / Fraction(b)))
            return q if isinstance(n.op, ast.FloorDiv) else a - b * q
        if isinstance(n, ast.Compare):
            parts, left = [], self.term(n.left, row, rows)
            for op, rn in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    vals = [self.term(e, row, rows) for e in rn.elts]
                    if left is None:
                        parts.append(None)
                        continue
                    if is_z3(left):
                        res = z3.Or(*[left == z3val(v) for v in vals]) if vals else z3.BoolVal(False)
                        parts.append(z3.Not(res) if isinstance(op, ast.NotIn) else res)
                    else:
                        res = any(left == v for v in vals)
                        parts.append(res if isinstance(op, ast.In) else not res)
                    continue
                right = self.term(rn, row, rows)
                if left is None or right is None:
                    parts.append(None)
                elif is_z3(left) or is_z3(right):
                    a, b = (real(left), real(right)) if not (isinstance(left, bool) or isinstance(right, bool)
                                                            or (is_z3(left) and z3.is_bool(left))) else (z3val(left), z3val(right))
                    parts.append({ast.Eq: a == b, ast.NotEq: a != b, ast.Lt: a < b, ast.LtE: a <= b,
                                  ast.Gt: a > b, ast.GtE: a >= b}[type(op)])
                else:
                    parts.append({ast.Eq: left == right, ast.NotEq: left != right, ast.Lt: left < right,
                                  ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right}[type(op)])
                left = right
            if any(is_z3(p) for p in parts):
                return z3.And(*[as_bool(p) for p in parts]) if len(parts) > 1 else as_bool(parts[0])
            if any(p is False for p in parts):
                return False
            return None if any(p is None for p in parts) else True
        if isinstance(n, ast.IfExp):
            c = self.term(n.test, row, rows)
            a, b = self.term(n.body, row, rows), self.term(n.orelse, row, rows)
            if is_z3(c):
                if a is None or b is None:
                    raise Z3Error("a branch of `x if c else y` is missing while c depends on a variable")
                return z3.If(c, real(a) if not isinstance(a, bool) and not (is_z3(a) and z3.is_bool(a)) else z3val(a),
                             real(b) if not isinstance(b, bool) and not (is_z3(b) and z3.is_bool(b)) else z3val(b))
            return a if c is True else b
        if isinstance(n, ast.Call):
            return self.call(n, row, rows)
        raise Z3Error(f"cannot translate {type(n).__name__}")

    def call(self, n: ast.Call, row, rows):
        name = n.func.id
        if name in AGGREGATES:
            if id(n) in self.agg_values:
                return self.const(self.agg_values[id(n)])
            if rows is None:
                raise Z3Error(f"aggregate `{ast.unparse(n)}` needs rows (it depends on a variable)")
            return self.symbolic_aggregate(n, rows)
        a = [self.term(x, row, rows) for x in n.args]
        sym = any(is_z3(v) for v in a)
        if name == "implies":
            if sym:
                return z3.Implies(as_bool(a[0]), as_bool(a[1]))
            na = None if a[0] is None else (not a[0])
            return True if (na is True or a[1] is True) else (None if None in (na, a[1]) else False)
        if name == "ite":
            if is_z3(a[0]):
                return z3.If(a[0], z3val(a[1]) if a[1] is not None else None, z3val(a[2]) if a[2] is not None else None)
            return a[1] if a[0] is True else a[2]
        if name == "abs":
            if a[0] is None:
                return None
            return z3.If(real(a[0]) >= 0, real(a[0]), -real(a[0])) if sym else abs(a[0])
        if name in ("min", "max"):
            if None in a:
                return None
            if sym:
                x, y = real(a[0]), real(a[1])
                return z3.If(x <= y, x, y) if name == "min" else z3.If(x >= y, x, y)
            return min(a) if name == "min" else max(a)
        if name == "coalesce":
            return a[1] if a[0] is None else a[0]
        if sym:
            raise Z3Error(f"{name}() cannot use solver variables")
        if name == "contains":
            return None if None in a else a[1].lower() in a[0].lower()
        if name == "startswith":
            return None if None in a else a[0].lower().startswith(a[1].lower())
        if name == "lower":
            return None if a[0] is None else a[0].lower()
        if name == "year_of":
            return None if a[0] is None else Fraction(int(str(a[0])[:4]))
        if name == "is_null":
            return a[0] is None
        if name == "not_null":
            return a[0] is not None
        raise Z3Error(f"unknown function `{name}()`")

    def symbolic_aggregate(self, n: ast.Call, rows: List):
        name, args = n.func.id, n.args
        if name in ("count", "share", "any_row", "all_rows"):
            flags = [as_bool(self.term(args[0], r, None)) if args else z3.BoolVal(True) for r in rows]
            if name == "any_row":
                return z3.Or(*flags) if flags else z3.BoolVal(False)
            if name == "all_rows":
                return z3.And(*flags) if flags else z3.BoolVal(True)
            total = z3.Sum([z3.If(f, z3.IntVal(1), z3.IntVal(0)) for f in flags]) if flags else z3.IntVal(0)
            return total if name == "count" else (z3.ToReal(total) / len(flags) if flags else None)
        if name != "sum":
            raise Z3Error(f"{name}() over rows cannot depend on a variable; use sum() and count()")
        terms = []
        for r in rows:
            c = as_bool(self.term(args[1], r, None)) if len(args) > 1 else None
            v = self.term(args[0], r, None)
            if v is None:
                continue
            terms.append(real(v) if c is None else z3.If(c, real(v), z3.RealVal(0)))
        return z3.Sum(terms) if terms else z3.RealVal(0)
