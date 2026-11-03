"""Model -> SQL that runs inside the lake, with exact arithmetic.

Every number is compiled to a pair (numerator, denominator) of integer SQL expressions. Integer columns
are themselves, DECIMAL(p,s) columns become x*10^s over 10^s, DOUBLE columns are read at a fixed scale.
Sums of a constant-denominator value are SUM(numerator) / denominator, so the lake returns exact integers;
comparisons never divide (a/b > c/d is decided as sign(a*d - c*b) * sign(b*d) > 0); overflow raises an error
instead of rounding. The only inexact case (an aggregate over a per-row varying denominator) is flagged.

Missing values follow SQL three-valued logic: a comparison with a missing value is not true.
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Tuple

from ..dsl import AGGREGATES
from ..dsl.parse import Expr, parse, ref_name
from ..ontology.model import Field, Ontology
from ..semantic.model import Scope, SemanticModel

INT_TYPES = ("BIGINT", "INTEGER", "INT", "HUGEINT", "SMALLINT", "TINYINT", "UBIGINT", "UINTEGER", "USMALLINT",
             "UTINYINT", "INT4", "INT8", "LONG")
FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL", "FLOAT8", "FLOAT4")


class CompileError(ValueError):
    pass


@dataclass
class R:
    kind: str                        # num | bool | text | null
    sql: str                         # numerator (num) or the expression itself
    den: str = "1"
    den_const: Optional[int] = 1     # positive integer when the denominator is a constant
    exact: bool = True


@dataclass
class AggItem:
    node_id: int
    label: str
    columns: List[str]               # SQL select expressions
    finalize: Callable[[list], object]
    exact: bool = True
    skipped_index: Optional[int] = None   # position in `columns` of the missing-value count


def _cmp_sym(op) -> str:
    return {ast.Eq: "=", ast.NotEq: "<>", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[type(op)]


class Compiler:
    def __init__(self, onto: Ontology, model: SemanticModel, engine, params: Dict[str, object],
                 variables: Optional[Dict[str, object]] = None, double_scale: int = 10):
        self.onto = onto
        self.m = model
        self.engine = engine
        self.d = engine.dialect
        self.params = params
        self.vars = variables or {}
        self.scale = double_scale
        self.scope = Scope(onto, model.universe, model.defines, params, self.vars)
        self._field_cache: Dict[Tuple[str, str], R] = {}
        self._agg_counter = 0

    # ------------------------------------------------------------------ names
    @staticmethod
    def alias_sql(alias: str) -> str:
        return "t0" if alias == "" else f"j_{alias}"

    def field_r(self, alias: str, f: Field) -> R:
        key = (alias, f.name)
        if key in self._field_cache:
            return self._field_cache[key]
        if f.expression:
            sub = _AliasScope(self, alias)
            r = sub.expr(parse(f.expression), row=True)
        else:
            col = f"{self.alias_sql(alias)}.{self.d.q(f.column)}"
            ptype = (getattr(f, "physical_type", None) or "").upper()
            if f.dtype == "text":
                r = R("text", col)
            elif f.dtype == "date":
                r = R("text", self.d.date_text(col))
            elif f.dtype == "bool":
                r = R("bool", col)
            elif ptype.startswith(INT_TYPES):
                r = R("num", self.d.to_int(col))
            elif ptype.startswith("DECIMAL") or ptype.startswith("NUMERIC"):
                s = int(ptype.split(",")[1].rstrip(")")) if "," in ptype else 0
                r = R("num", self.d.to_int(f"{col} * {10 ** s}") if s else self.d.to_int(col), self._i(10 ** s), 10 ** s)
            elif ptype.startswith(FLOAT_TYPES) or ptype == "":
                r = R("num", self.d.double_to_int(col, self.scale), self._i(10 ** self.scale), 10 ** self.scale)
            else:
                raise CompileError(f"field `{f.entity}.{f.name}` has physical type {ptype}, not a number")
        self._field_cache[key] = r
        return r

    def name_r(self, name: str, row: bool) -> R:
        kind = self.scope.kind(name)
        if kind == "param":
            return self.literal(self.params[name])
        if kind == "var":
            return self.literal(self.vars[name])
        if kind == "define":
            return self.expr(self.m.defines[name], row=True)
        if kind == "field":
            alias, f = self.scope.field(name)
            return self.field_r(alias, f)
        raise CompileError(f"unknown name `{name}`")

    def literal(self, v) -> R:
        if v is None:
            return R("null", "NULL")
        if isinstance(v, bool):
            return R("bool", "TRUE" if v else "FALSE")
        if isinstance(v, str):
            return R("text", self.d.lit_str(v))
        # floats through their shortest repr (0.05 is 1/20, not the binary double); every integer literal is cast
        # to the 128-bit type, because the engine types bare literals as 64-bit and literal*literal can overflow
        f = Fraction(repr(v)) if isinstance(v, float) else Fraction(v)
        return R("num", self.d.to_int(str(f.numerator)), self._i(f.denominator), f.denominator)

    # ------------------------------------------------------------------ expressions
    def expr(self, e: Expr, row: bool = True) -> R:
        return self.node(e.body, row)

    def node(self, n, row: bool) -> R:
        if isinstance(n, ast.Constant):
            return self.literal(n.value)
        if isinstance(n, (ast.Name, ast.Attribute)):
            return self.name_r(ref_name(n), row)
        if isinstance(n, ast.UnaryOp):
            a = self.node(n.operand, row)
            if isinstance(n.op, ast.Not):
                return R("bool", f"(NOT {self.as_bool(a)})")
            if isinstance(n.op, ast.USub):
                c = self._const(a)
                if c is not None:
                    return self.literal(-c)
                return R("num", f"(-({a.sql}))", a.den, a.den_const, a.exact)
            return a
        if isinstance(n, ast.BoolOp):
            parts = [self.as_bool(self.node(v, row)) for v in n.values]
            return R("bool", "(" + (" AND " if isinstance(n.op, ast.And) else " OR ").join(parts) + ")")
        if isinstance(n, ast.BinOp):
            return self.binop(n.op, self.node(n.left, row), self.node(n.right, row), n)
        if isinstance(n, ast.Compare):
            return self.compare(n, row)
        if isinstance(n, ast.IfExp):
            return self.choose(self.as_bool(self.node(n.test, row)), self.node(n.body, row), self.node(n.orelse, row))
        if isinstance(n, ast.Call):
            return self.call(n, row)
        raise CompileError(f"cannot compile {type(n).__name__}")

    def as_bool(self, r: R) -> str:
        if r.kind not in ("bool", "null"):
            raise CompileError(f"expected a condition, got {r.kind}")
        return r.sql

    def num(self, r: R) -> R:
        if r.kind == "null":
            return R("num", f"CAST(NULL AS {self.d.int_type})")
        if r.kind != "num":
            raise CompileError(f"expected a number, got {r.kind}")
        return r

    def _i(self, k: int) -> str:
        """An integer constant in SQL, always as the 128-bit type (bare literals are 64-bit in most engines)."""
        return self.d.to_int(str(int(k)))

    def _scaled(self, sql: str, k: int) -> str:
        return sql if k == 1 else f"({sql}) * {self.d.to_int(str(k))}"

    def add(self, a: R, b: R, minus: bool) -> R:
        a, b = self.num(a), self.num(b)
        op = "-" if minus else "+"
        exact = a.exact and b.exact
        if a.den_const and b.den_const:
            L = a.den_const * b.den_const // math.gcd(a.den_const, b.den_const)
            return R("num", f"({self._scaled(a.sql, L // a.den_const)} {op} {self._scaled(b.sql, L // b.den_const)})",
                     self._i(L), L, exact)
        return R("num", f"(({a.sql}) * ({b.den}) {op} ({b.sql}) * ({a.den}))", f"(({a.den}) * ({b.den}))", None, exact)

    def binop(self, op, a: R, b: R, node) -> R:
        if isinstance(op, (ast.Add, ast.Sub)):
            return self.add(a, b, isinstance(op, ast.Sub))
        a, b = self.num(a), self.num(b)
        exact = a.exact and b.exact
        if isinstance(op, ast.Mult):
            den_c = a.den_const * b.den_const if a.den_const and b.den_const else None
            den = self._i(den_c) if den_c else f"(({a.den}) * ({b.den}))"
            return R("num", f"(({a.sql}) * ({b.sql}))", den, den_c, exact)
        if isinstance(op, ast.Div):
            lit = self._const(b)
            if lit is not None:
                if lit == 0:
                    return R("num", f"CAST(NULL AS {self.d.int_type})")
                p, q = lit.numerator, lit.denominator
                sign = "-" if p < 0 else ""
                num = f"({sign}({a.sql}) * {self._i(q)})" if q != 1 or sign else a.sql
                if a.den_const:
                    return R("num", num, self._i(a.den_const * abs(p)), a.den_const * abs(p), exact)
                return R("num", num, f"(({a.den}) * {self._i(abs(p))})", None, exact)
            return R("num", f"(({a.sql}) * ({b.den}))", f"(({a.den}) * NULLIF({b.sql}, 0))", None, exact)
        # // and % by a positive whole-number literal
        k = self._const(b)
        if k is None or k <= 0 or k.denominator != 1:
            raise CompileError("`//` and `%` need a positive whole-number literal divisor")
        if not a.den_const:
            raise CompileError("`//` and `%` need a value with a fixed denominator")
        m = a.den_const * int(k)
        mod_pos = self.d.mod(f"{self.d.mod(a.sql, self._i(m))} + {self._i(m)}", self._i(m))
        if isinstance(op, ast.FloorDiv):
            return R("num", self.d.exact_div(f"({a.sql}) - {mod_pos}", self._i(m)), self._i(1), 1, exact)
        return R("num", mod_pos, self._i(a.den_const), a.den_const, exact)

    def _const(self, r: R) -> Optional[Fraction]:
        """The value of a literal R (an integer numerator over a constant denominator), else None."""
        if r.kind != "num" or not r.den_const:
            return None
        s = r.sql.strip()
        if s.startswith("CAST(") and s.endswith(f" AS {self.d.int_type})"):
            s = s[5:-len(f" AS {self.d.int_type})")]
        try:
            return Fraction(int(s), r.den_const)
        except ValueError:
            return None

    def compare_pair(self, op, a: R, b: R) -> str:
        if isinstance(op, (ast.In, ast.NotIn)):
            raise CompileError("internal: `in` handled by compare()")
        sym = _cmp_sym(op)
        if a.kind == "num" or b.kind == "num":
            diff = self.add(a, b, minus=True)
            if diff.den_const:
                return f"(({diff.sql}) {sym} 0)"
            if sym in ("=", "<>"):
                return f"(({diff.sql}) {sym} 0)"
            return f"((SIGN({diff.sql}) * SIGN({diff.den})) {sym} 0)"
        return f"(({a.sql}) {sym} ({b.sql}))"

    def compare(self, n: ast.Compare, row: bool) -> R:
        parts, left = [], self.node(n.left, row)
        for op, rnode in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                vals = [self.literal(e.value) for e in rnode.elts]
                if not vals:
                    parts.append("FALSE" if isinstance(op, ast.In) else "TRUE")
                    continue
                if left.kind == "num":
                    ors = " OR ".join(self.compare_pair(ast.Eq(), left, v) for v in vals)
                    inner = f"({ors})"
                else:
                    inner = f"(({left.sql}) IN ({', '.join(v.sql for v in vals)}))"
                parts.append(inner if isinstance(op, ast.In) else f"(NOT {inner})")
                continue
            right = self.node(rnode, row)
            parts.append(self.compare_pair(op, left, right))
            left = right
        return R("bool", parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")")

    def choose(self, cond: str, a: R, b: R) -> R:
        if a.kind == "null" and b.kind != "null":
            a = R(b.kind, f"CAST(NULL AS {self.d.int_type})" if b.kind == "num" else "NULL", b.den, b.den_const)
        if b.kind == "null" and a.kind != "null":
            b = R(a.kind, f"CAST(NULL AS {self.d.int_type})" if a.kind == "num" else "NULL", a.den, a.den_const)
        if a.kind != "num":
            return R(a.kind, f"(CASE WHEN {cond} THEN {a.sql} ELSE {b.sql} END)")
        exact = a.exact and b.exact
        if a.den_const and b.den_const:
            L = a.den_const * b.den_const // math.gcd(a.den_const, b.den_const)
            return R("num", f"(CASE WHEN {cond} THEN {self._scaled(a.sql, L // a.den_const)} "
                            f"ELSE {self._scaled(b.sql, L // b.den_const)} END)", self._i(L), L, exact)
        return R("num", f"(CASE WHEN {cond} THEN {a.sql} ELSE {b.sql} END)",
                 f"(CASE WHEN {cond} THEN {a.den} ELSE {b.den} END)", None, exact)

    def is_null(self, r: R) -> str:
        return f"(({r.sql}) IS NULL)"

    def call(self, n: ast.Call, row: bool) -> R:
        name = n.func.id
        if name in AGGREGATES:
            raise CompileError(f"aggregate {name}() must be planned with aggregate(), not compiled inline")
        args = [self.node(a, row) for a in n.args]
        if name == "implies":
            return R("bool", f"((NOT {self.as_bool(args[0])}) OR {self.as_bool(args[1])})")
        if name == "ite":
            return self.choose(self.as_bool(args[0]), args[1], args[2])
        if name == "abs":
            a = self.num(args[0])
            if a.den_const:
                return R("num", f"ABS({a.sql})", a.den, a.den_const, a.exact)
            return R("num", f"ABS({a.sql})", f"ABS({a.den})", None, a.exact)
        if name in ("min", "max"):
            a, b = args
            pick_a = self.compare_pair(ast.LtE() if name == "min" else ast.GtE(), a, b)
            either_null = f"({self.is_null(a)} OR {self.is_null(b)})"
            chosen = self.choose(pick_a, a, b)
            if chosen.kind == "num":
                return R("num", f"(CASE WHEN {either_null} THEN NULL ELSE {chosen.sql} END)", chosen.den,
                         chosen.den_const, chosen.exact)
            return R(chosen.kind, f"(CASE WHEN {either_null} THEN NULL ELSE {chosen.sql} END)")
        if name == "coalesce":
            return self.choose(f"(NOT {self.is_null(args[0])})", args[0], args[1])
        if name == "contains":
            return R("bool", self.d.contains(args[0].sql, args[1].sql))
        if name == "startswith":
            return R("bool", self.d.starts_with(args[0].sql, args[1].sql))
        if name == "lower":
            return R("text", f"lower({args[0].sql})")
        if name == "year_of":
            return R("num", self.d.to_int(f"SUBSTR({args[0].sql}, 1, 4)"))
        if name == "is_null":
            return R("bool", self.is_null(args[0]))
        if name == "not_null":
            return R("bool", f"(({args[0].sql}) IS NOT NULL)")
        raise CompileError(f"function `{name}()` cannot be compiled to SQL")

    # ------------------------------------------------------------------ aggregates
    def aggregate(self, call: ast.Call) -> AggItem:
        name = call.func.id
        label = ast.unparse(call)
        d = self.d
        args = call.args
        if name in ("count", "share", "any_row", "all_rows"):
            cond = self.as_bool(self.node(args[0], True)) if args else None
            hit = d.count_if(cond) if cond else "COUNT(*)"
            if name == "count":
                return AggItem(id(call), label, [hit], lambda v: Fraction(v[0] or 0))
            if name == "any_row":
                return AggItem(id(call), label, [hit], lambda v: (v[0] or 0) > 0)
            fin = (lambda v: None if not v[1] else Fraction(v[0] or 0, v[1])) if name == "share" else \
                (lambda v: (v[0] or 0) == (v[1] or 0))
            return AggItem(id(call), label, [hit, "COUNT(*)"], fin)
        x = self.node(args[0], True)
        cond = self.as_bool(self.node(args[1], True)) if len(args) > 1 else "TRUE"
        filt = lambda fn, e, c: self.agg_filter(fn, e, c)
        skipped = d.count_if(f"({cond}) AND (({x.sql}) IS NULL)")
        if x.kind == "text":
            fn = "MIN" if name == "min_of" else "MAX"
            if name not in ("min_of", "max_of"):
                raise CompileError(f"{name}() needs numbers")
            return AggItem(id(call), label, [filt(fn, x.sql, cond), skipped], lambda v: v[0], True, 1)
        x = self.num(x)
        if x.den_const:
            D = x.den_const
            if name == "sum":
                return AggItem(id(call), label, [filt("SUM", x.sql, cond), skipped],
                               lambda v: Fraction(int(v[0] or 0), D), x.exact, 1)
            if name == "avg":
                return AggItem(id(call), label, [filt("SUM", x.sql, cond), filt("COUNT", x.sql, cond), skipped],
                               lambda v: None if not v[1] else Fraction(int(v[0]), int(v[1]) * D), x.exact, 2)
            fn = "MIN" if name == "min_of" else "MAX"
            return AggItem(id(call), label, [filt(fn, x.sql, cond), skipped],
                           lambda v: None if v[0] is None else Fraction(int(v[0]), D), x.exact, 1)
        # per-row varying denominator: computed in floating point and flagged as inexact
        val = f"({d.to_double(x.sql)} / {d.to_double(x.den)})"
        fn = {"sum": "SUM", "avg": "AVG", "min_of": "MIN", "max_of": "MAX"}[name]
        conv = lambda v: None if v is None else Fraction(repr(float(v)))
        if name == "sum":
            return AggItem(id(call), label, [filt(fn, val, cond), skipped], lambda v: conv(v[0] or 0.0), False, 1)
        return AggItem(id(call), label, [filt(fn, val, cond), skipped], lambda v: conv(v[0]), False, 1)

    def agg_filter(self, fn: str, expr: str, cond: str) -> str:
        if cond == "TRUE":
            return f"{fn}({expr})"
        if self.d.name == "snowflake":
            return f"{fn}(CASE WHEN {cond} THEN {expr} END)"
        return f"{fn}({expr}) FILTER (WHERE {cond})"

    # ------------------------------------------------------------------ query shapes
    def from_clause(self) -> str:
        u = self.m.universe
        ref = self.engine.table_ref
        sql = f"{ref(self.onto.entities[u.entity].table)} t0"
        for j in u.joins:
            kind = "LEFT JOIN" if j.cardinality in ("many_to_one", "one_to_one") else "JOIN"
            ents = u.aliases
            conds = []
            for a, b in j.pairs:
                fa, fb = self.onto.field(ents[j.from_alias], a), self.onto.field(j.entity, b)
                if not (fa and fa.column and fb and fb.column):
                    raise CompileError(f"join `{j.alias}`: join keys must be column fields")
                conds.append(f"{self.alias_sql(j.from_alias)}.{self.d.q(fa.column)} = {self.alias_sql(j.alias)}."
                             f"{self.d.q(fb.column)}")
            sql += f" {kind} {ref(self.onto.entities[j.entity].table)} {self.alias_sql(j.alias)} ON {' AND '.join(conds)}"
        return sql

    def where_clause(self, extra: Optional[str] = None, include_rows: bool = True) -> str:
        parts = []
        for alias, pol in self.m.policies.items():
            parts.append(self.as_bool(_AliasScope(self, alias).expr(pol, row=True)))
        if include_rows and self.m.rows is not None:
            parts.append(self.as_bool(self.expr(self.m.rows)))
        if extra:
            parts.append(extra)
        return " AND ".join(f"COALESCE({p}, FALSE)" for p in parts) if parts else "TRUE"

    def aggregate_query(self, items: List[AggItem], group: Optional[R] = None) -> str:
        cols = []
        if group is not None:
            cols.append(f"{group.sql} AS g")
        for i, it in enumerate(items):
            for k, c in enumerate(it.columns):
                cols.append(f"{c} AS a{i}_{k}")
        sql = f"SELECT {', '.join(cols) or '1'} FROM {self.from_clause()} WHERE {self.where_clause()}"
        if group is not None:
            sql += " GROUP BY 1"
        return sql

    def group_expr(self) -> R:
        g = self.expr(self.m.group_by)
        if g.kind == "num" and not g.den_const:
            raise CompileError("group_by must have a fixed denominator (e.g. year // 10 * 10), not a per-row ratio")
        return g


class _AliasScope(Compiler):
    """Compiles field expressions and row policies of one alias: bare names are that alias's fields."""

    def __init__(self, parent: Compiler, alias: str):
        self.__dict__.update(parent.__dict__)
        self.parent = parent
        self.alias = alias
        self.entity = parent.m.universe.aliases[alias]

    def name_r(self, name: str, row: bool) -> R:
        f = self.onto.field(self.entity, name)
        if f is None:
            raise CompileError(f"`{name}` is not a field of `{self.entity}`")
        return self.parent.field_r(self.alias, f)
