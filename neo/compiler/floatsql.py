"""A second, deliberately different compilation of the same model: plain SQL with floating-point arithmetic.

Used as the independent check when a universe is too large for the exact Python interpreter. It shares no
code with the exact compiler (no numerator/denominator pairs, native three-valued logic), so the two
disagreeing reveals a compiler or engine problem. Its numbers are approximate; the prover compares counts
exactly and amounts with a relative tolerance, and labels the check as approximate in the certificate.
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional

from ..dsl import AGGREGATES
from ..dsl.parse import Expr, parse, ref_name
from ..ontology.model import Field
from ..semantic.model import Scope, SemanticModel
from .sql import CompileError


class FloatCompiler:
    def __init__(self, onto, model: SemanticModel, engine, params: Dict[str, object]):
        self.onto, self.m, self.engine, self.d = onto, model, engine, engine.dialect
        self.params = params
        self.scope = Scope(onto, model.universe, model.defines, params, {})

    def a(self, alias: str) -> str:
        return "f0" if alias == "" else f"fj_{alias}"

    def field(self, alias: str, f: Field) -> str:
        if f.expression:
            return _Sub(self, alias).node(parse(f.expression).body)
        col = f"{self.a(alias)}.{self.d.q(f.column)}"
        if f.dtype == "date":
            return self.d.date_text(col)
        if f.dtype in ("text", "bool"):
            return col
        return f"CAST({col} AS DOUBLE)"

    def lit(self, v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, str):
            return self.d.lit_str(v)
        return f"CAST({float(v)!r} AS DOUBLE)"

    def name(self, nm: str) -> str:
        kind = self.scope.kind(nm)
        if kind == "param":
            return self.lit(self.params[nm])
        if kind == "define":
            return self.node(self.m.defines[nm].body)
        if kind == "field":
            alias, f = self.scope.field(nm)
            return self.field(alias, f)
        raise CompileError(f"unknown name `{nm}`")

    def node(self, n) -> str:
        if isinstance(n, ast.Constant):
            return self.lit(n.value)
        if isinstance(n, (ast.Name, ast.Attribute)):
            return self.name(ref_name(n))
        if isinstance(n, ast.UnaryOp):
            v = self.node(n.operand)
            return f"(NOT {v})" if isinstance(n.op, ast.Not) else (f"(-{v})" if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BoolOp):
            return "(" + (" AND " if isinstance(n.op, ast.And) else " OR ").join(self.node(v) for v in n.values) + ")"
        if isinstance(n, ast.BinOp):
            a, b = self.node(n.left), self.node(n.right)
            if isinstance(n.op, ast.Div):
                return f"({a} / NULLIF({b}, 0))"
            if isinstance(n.op, ast.FloorDiv):
                return f"FLOOR({a} / {b})"
            if isinstance(n.op, ast.Mod):
                return f"({a} - {b} * FLOOR({a} / {b}))"
            return f"({a} {dict([(ast.Add, '+'), (ast.Sub, '-'), (ast.Mult, '*')])[type(n.op)]} {b})"
        if isinstance(n, ast.Compare):
            parts, left = [], self.node(n.left)
            for op, rn in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    vals = ", ".join(self.lit(e.value) for e in rn.elts) or "NULL"
                    parts.append(f"({left} {'IN' if isinstance(op, ast.In) else 'NOT IN'} ({vals}))")
                    continue
                right = self.node(rn)
                sym = {ast.Eq: "=", ast.NotEq: "<>", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[type(op)]
                parts.append(f"({left} {sym} {right})")
                left = right
            return "(" + " AND ".join(parts) + ")"
        if isinstance(n, ast.IfExp):
            return f"(CASE WHEN {self.node(n.test)} THEN {self.node(n.body)} ELSE {self.node(n.orelse)} END)"
        if isinstance(n, ast.Call):
            nm, args = n.func.id, [self.node(x) for x in n.args]
            simple = {"abs": "ABS({0})", "lower": "lower({0})", "is_null": "({0} IS NULL)",
                      "not_null": "({0} IS NOT NULL)", "coalesce": "COALESCE({0}, {1})",
                      "implies": "((NOT {0}) OR {1})", "ite": "(CASE WHEN {0} THEN {1} ELSE {2} END)",
                      "year_of": "CAST(SUBSTR({0}, 1, 4) AS DOUBLE)",
                      "min": "(CASE WHEN {0} IS NULL OR {1} IS NULL THEN NULL WHEN {0} <= {1} THEN {0} ELSE {1} END)",
                      "max": "(CASE WHEN {0} IS NULL OR {1} IS NULL THEN NULL WHEN {0} >= {1} THEN {0} ELSE {1} END)"}
            if nm in simple:
                return simple[nm].format(*args)
            if nm == "contains":
                return self.d.contains(args[0], args[1])
            if nm == "startswith":
                return self.d.starts_with(args[0], args[1])
            if nm in AGGREGATES:
                return self.agg(nm, n.args)
        raise CompileError(f"float compiler cannot compile {ast.unparse(n)}")

    def agg(self, nm: str, args) -> str:
        cond = self.node(args[0]) if nm in ("count", "share", "any_row", "all_rows") and args else None
        if nm == "count":
            return f"COUNT(*) FILTER (WHERE {cond})" if cond else "CAST(COUNT(*) AS DOUBLE)"
        if nm == "share":
            return f"(CAST(COUNT(*) FILTER (WHERE {cond}) AS DOUBLE) / NULLIF(COUNT(*), 0))"
        if nm == "any_row":
            return f"(COUNT(*) FILTER (WHERE {cond}) > 0)"
        if nm == "all_rows":
            return f"(COUNT(*) FILTER (WHERE {cond}) = COUNT(*))"
        x = self.node(args[0])
        fn = {"sum": "SUM", "avg": "AVG", "min_of": "MIN", "max_of": "MAX"}[nm]
        if len(args) > 1:
            return f"{fn}({x}) FILTER (WHERE {self.node(args[1])})"
        return f"COALESCE(SUM({x}), 0)" if nm == "sum" else f"{fn}({x})"

    def from_where(self) -> str:
        u = self.m.universe
        sql = f"{self.engine.table_ref(self.onto.entities[u.entity].table)} f0"
        for j in u.joins:
            kind = "LEFT JOIN" if j.cardinality in ("many_to_one", "one_to_one") else "JOIN"
            conds = " AND ".join(
                f"{self.a(j.from_alias)}.{self.d.q(self.onto.field(u.aliases[j.from_alias], x).column)} = "
                f"{self.a(j.alias)}.{self.d.q(self.onto.field(j.entity, y).column)}" for x, y in j.pairs)
            sql += f" {kind} {self.engine.table_ref(self.onto.entities[j.entity].table)} {self.a(j.alias)} ON {conds}"
        parts = [_Sub(self, alias).node(pol.body) for alias, pol in self.m.policies.items()]
        if self.m.rows is not None:
            parts.append(self.node(self.m.rows.body))
        return sql + " WHERE " + (" AND ".join(f"COALESCE({p}, FALSE)" for p in parts) if parts else "TRUE")

    def query(self, exprs: Dict[str, Expr], grouped: bool = False) -> str:
        cols = [f"{self.node(e.body)} AS {k}" for k, e in exprs.items()]
        if grouped:
            cols.insert(0, f"{self.node(self.m.group_by.body)} AS g")
        sql = f"SELECT {', '.join(cols)} FROM {self.from_where()}"
        return sql + (" GROUP BY 1" if grouped else "")


class _Sub(FloatCompiler):
    def __init__(self, parent: FloatCompiler, alias: str):
        self.__dict__.update(parent.__dict__)
        self.parent, self.alias = parent, alias
        self.entity = parent.m.universe.aliases[alias]

    def name(self, nm: str) -> str:
        f = self.onto.field(self.entity, nm)
        if f is None:
            raise CompileError(f"`{nm}` is not a field of `{self.entity}`")
        return self.parent.field(self.alias, f)
