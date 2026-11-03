"""The independent exact interpreter: the second implementation of the model's semantics.

It reads only raw columns from the pinned snapshot (the engine is used as a storage reader, no
expressions are pushed down), performs the joins, filters and aggregates in Python with exact fractions
and SQL three-valued logic, and shares no code with the SQL compiler. When both paths agree, a compiler
bug or an engine arithmetic quirk would have had to produce the same wrong number twice.

It refuses universes larger than `verification.exact_max_rows`; above that the prover uses the second
engine path instead and says so in the certificate.
"""
from __future__ import annotations

import ast
import datetime as dt
import decimal
import math
from collections import defaultdict
from fractions import Fraction
from typing import Dict, List, Optional, Set, Tuple

from ..dsl import AGGREGATES
from ..dsl.parse import Expr, parse, ref_name, refs
from ..ontology.model import Field, Ontology
from ..semantic.model import Scope, SemanticModel
from ..util import double_to_fraction


class TooLarge(RuntimeError):
    pass


class InterpError(ValueError):
    pass


def _k_and(vals):
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def _k_or(vals):
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def _k_not(v):
    return None if v is None else (not v)


class Interpreter:
    def __init__(self, onto: Ontology, model: SemanticModel, engine, params: Dict[str, object],
                 double_scale: int = 10, max_rows: int = 300_000, variables: Optional[dict] = None,
                 rules=None, agg_override: Optional[Dict[int, object]] = None):
        self.onto = onto
        self.m = model
        self.engine = engine
        self.params = params
        self.vars = variables or {}
        self.rules = rules
        self.agg_override = agg_override or {}
        self.scale = double_scale
        self.max_rows = max_rows
        self.scope = Scope(onto, model.universe, model.defines, params, self.vars)
        self.rows: List[Dict[str, dict]] = []
        self.skipped: Dict[str, int] = defaultdict(int)
        self._loaded = False

    # ------------------------------------------------------------------ loading
    def _columns_needed(self) -> Dict[str, Set[str]]:
        need: Dict[str, Set[str]] = defaultdict(set)
        u = self.m.universe
        exprs: List[Expr] = [e for e in [self.m.rows, self.m.group_by] if e is not None]
        exprs += list(self.m.defines.values()) + [p.expr for p in self.m.params]
        exprs += [c.expr for c in self.m.constraints + self.m.assumptions]
        t = self.m.target
        for k in ("expr", "claim", "objective", "where"):
            if isinstance(t.get(k), Expr):
                exprs.append(t[k])
        exprs += list((t.get("columns") or {}).values())
        exprs += [parse(s) for s in (t.get("select") or [])]
        seen_defines = set()

        def walk(e: Expr, alias: Optional[str]):
            for nm in refs(e)[0]:
                if alias is not None:
                    f = self.onto.field(u.aliases[alias], nm)
                    if f:
                        add_field(alias, f)
                    continue
                kind = self.scope.kind(nm)
                if kind == "define" and nm not in seen_defines:
                    seen_defines.add(nm)
                    walk(self.m.defines[nm], None)
                elif kind == "field":
                    a, f = self.scope.field(nm)
                    add_field(a, f)

        def add_field(alias: str, f: Field):
            if f.column:
                need[alias].add(f.column)
            else:
                walk(parse(f.expression), alias)

        for e in exprs:
            walk(e, None)
        for alias, pol in self.m.policies.items():
            walk(pol, alias)
        ents = u.aliases
        for j in u.joins:
            for a, b in j.pairs:
                need[j.from_alias].add(self.onto.field(ents[j.from_alias], a).column)
                need[j.alias].add(self.onto.field(j.entity, b).column)
        key = self.onto.entities[u.entity].key
        kf = self.onto.field(u.entity, key) if key else None
        if kf and kf.column:
            need[""].add(kf.column)
        return need

    def _convert(self, f: Field, v):
        if v is None:
            return None
        if f.dtype == "text":
            return str(v)
        if f.dtype == "date":
            if isinstance(v, dt.datetime):
                return v.date().isoformat()
            if isinstance(v, dt.date):
                return v.isoformat()
            return str(v)[:10]
        if f.dtype == "bool":
            return bool(v)
        if isinstance(v, bool):
            return Fraction(int(v))
        if isinstance(v, int):
            return Fraction(v)
        if isinstance(v, decimal.Decimal):
            return Fraction(v)
        if isinstance(v, float):
            if math.isnan(v):
                return None
            return double_to_fraction(v, self.scale)
        raise InterpError(f"unexpected value {v!r} for field {f.entity}.{f.name}")

    def _read(self, entity: str, columns: Set[str]) -> List[dict]:
        e = self.onto.entities[entity]
        cols = sorted(columns)
        by_col = {f.column: f for f in self.onto.fields_of(entity) if f.column}
        d = self.engine.dialect

        def read(c):
            # floating-point columns are read through the engine's decimal cast at a fixed scale (the definition
            # of their exact value, shared with the compiler); every other column is read as stored
            ptype = (getattr(by_col[c], "physical_type", "") or "").upper()
            if by_col[c].dtype == "number" and ptype.startswith(("DOUBLE", "FLOAT", "REAL")):
                return d.double_to_decimal(d.q(c), self.scale)
            return d.q(c)
        sql = f"SELECT {', '.join(read(c) for c in cols) or '1'} FROM {self.engine.table_ref(e.table)}"
        out = []
        for _, batch in self.engine.stream(sql):
            for r in batch:
                out.append({c: self._convert(by_col[c], v) for c, v in zip(cols, r)})
                if len(out) > self.max_rows:
                    raise TooLarge(f"`{e.table}` has more than {self.max_rows:,} rows")
        return out

    def load(self):
        if self._loaded:
            return
        u = self.m.universe
        need = self._columns_needed()
        combined = [{"": r} for r in self._read(u.entity, need[""])]
        for j in u.joins:
            child = self._read(j.entity, need[j.alias])
            ents = u.aliases
            from_cols = [self.onto.field(ents[j.from_alias], a).column for a, _ in j.pairs]
            to_cols = [self.onto.field(j.entity, b).column for _, b in j.pairs]
            index: Dict[tuple, list] = defaultdict(list)
            for c in child:
                key = tuple(c[k] for k in to_cols)
                if None not in key:
                    index[key].append(c)
            nxt = []
            for row in combined:
                key = tuple(row[j.from_alias][k] for k in from_cols)
                matches = index.get(key, []) if None not in key else []
                if j.cardinality in ("many_to_one", "one_to_one"):
                    nxt.append({**row, j.alias: matches[0] if matches else {k: None for k in need[j.alias]}})
                else:
                    nxt.extend({**row, j.alias: m} for m in matches)
                if len(nxt) > self.max_rows:
                    raise TooLarge(f"the joined universe has more than {self.max_rows:,} rows")
            combined = nxt
        self.rows = combined
        self._loaded = True

    # ------------------------------------------------------------------ evaluation
    def field_value(self, row: dict, alias: str, f: Field):
        if f.column:
            return row[alias].get(f.column)
        sub = _AliasEval(self, alias)
        return sub.ev(parse(f.expression).body, row, None)

    def name(self, nm: str, row: Optional[dict], rows):
        kind = self.scope.kind(nm)
        if kind == "param":
            return self.params[nm]
        if kind == "var":
            return self.vars[nm]
        if kind == "define":
            if row is None:
                raise InterpError(f"`{nm}` is per-row")
            return self.ev(self.m.defines[nm].body, row, None)
        if kind == "field":
            if row is None:
                raise InterpError(f"`{nm}` is per-row")
            alias, f = self.scope.field(nm)
            return self.field_value(row, alias, f)
        raise InterpError(f"unknown name `{nm}`")

    def ev(self, n, row: Optional[dict], rows: Optional[List[dict]]):
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return Fraction(repr(v)) if isinstance(v, float) else Fraction(v)
            return v
        if isinstance(n, (ast.Name, ast.Attribute)):
            return self.name(ref_name(n), row, rows)
        if isinstance(n, ast.UnaryOp):
            v = self.ev(n.operand, row, rows)
            if isinstance(n.op, ast.Not):
                return _k_not(v)
            return None if v is None else (-v if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BoolOp):
            vals = [self.ev(x, row, rows) for x in n.values]
            return _k_and(vals) if isinstance(n.op, ast.And) else _k_or(vals)
        if isinstance(n, ast.BinOp):
            a, b = self.ev(n.left, row, rows), self.ev(n.right, row, rows)
            if a is None or b is None:
                return None
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return None if b == 0 else Fraction(a) / Fraction(b)
            q = Fraction(math.floor(Fraction(a) / Fraction(b)))
            return q if isinstance(n.op, ast.FloorDiv) else a - b * q
        if isinstance(n, ast.Compare):
            out, left = [], self.ev(n.left, row, rows)
            for op, rn in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    vals = [self.ev(e, row, rows) for e in rn.elts]
                    res = None if left is None else any(left == v for v in vals)
                    out.append(res if isinstance(op, ast.In) else _k_not(res))
                    continue
                right = self.ev(rn, row, rows)
                if left is None or right is None:
                    out.append(None)
                else:
                    out.append({ast.Eq: left == right, ast.NotEq: left != right, ast.Lt: left < right,
                                ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right}[type(op)])
                left = right
            return _k_and(out)
        if isinstance(n, ast.IfExp):
            return self.ev(n.body, row, rows) if self.ev(n.test, row, rows) is True else self.ev(n.orelse, row, rows)
        if isinstance(n, ast.Call):
            return self.call(n, row, rows)
        raise InterpError(f"cannot evaluate {type(n).__name__}")

    def call(self, n: ast.Call, row, rows):
        name = n.func.id
        if name in AGGREGATES:
            if id(n) in self.agg_override:
                return self.agg_override[id(n)]
            if rows is None:
                raise InterpError(f"aggregate {name}() needs a set of rows")
            return self.aggregate(n, rows)
        if name == "rule_param":
            value, _ = self.rules.param(n.args[0].value, n.args[1].value)
            return value if isinstance(value, str) else Fraction(repr(value)) if isinstance(value, float) else Fraction(value)
        a = [self.ev(x, row, rows) for x in n.args]
        if name == "implies":
            return _k_or([_k_not(a[0]), a[1]])
        if name == "ite":
            return a[1] if a[0] is True else a[2]
        if name == "abs":
            return None if a[0] is None else abs(a[0])
        if name in ("min", "max"):
            if a[0] is None or a[1] is None:
                return None
            return (a[0] if a[0] <= a[1] else a[1]) if name == "min" else (a[0] if a[0] >= a[1] else a[1])
        if name == "coalesce":
            return a[1] if a[0] is None else a[0]
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
        raise InterpError(f"unknown function `{name}()`")

    def aggregate(self, n: ast.Call, rows: List[dict]):
        name, args = n.func.id, n.args
        label = ast.unparse(n)
        if name in ("count", "share", "any_row", "all_rows"):
            hits = sum(1 for r in rows if (self.ev(args[0], r, None) is True if args else True))
            if name == "count":
                return Fraction(hits)
            if name == "share":
                return Fraction(hits, len(rows)) if rows else None
            return hits > 0 if name == "any_row" else hits == len(rows)
        vals, skipped = [], 0
        for r in rows:
            if len(args) > 1 and self.ev(args[1], r, None) is not True:
                continue
            v = self.ev(args[0], r, None)
            if v is None:
                skipped += 1
            else:
                vals.append(v)
        self.skipped[label] = skipped
        if name == "sum":
            return sum(vals, Fraction(0))
        if not vals:
            return None
        if name == "avg":
            return sum(vals, Fraction(0)) / len(vals)
        return min(vals) if name == "min_of" else max(vals)

    # ------------------------------------------------------------------ entry points
    def eligible(self) -> List[dict]:
        self.load()
        out = []
        for r in self.rows:
            ok = all(_AliasEval(self, alias).ev(pol.body, r, None) is True for alias, pol in self.m.policies.items())
            if ok and (self.m.rows is None or self.ev(self.m.rows.body, r, None) is True):
                out.append(r)
        return out

    def whole(self, e: Expr, rows: List[dict]):
        return self.ev(e.body, None, rows)

    def row_value(self, e: Expr, row: dict):
        return self.ev(e.body, row, None)

    def groups(self, rows: List[dict]) -> Dict[object, List[dict]]:
        out: Dict[object, List[dict]] = defaultdict(list)
        for r in rows:
            out[self.ev(self.m.group_by.body, r, None)].append(r)
        return out


class _AliasEval(Interpreter):
    """Evaluates field expressions and row policies of one alias: bare names are that alias's fields."""

    def __init__(self, parent: Interpreter, alias: str):
        self.__dict__.update(parent.__dict__)
        self.parent = parent
        self.alias = alias
        self.entity = parent.m.universe.aliases[alias]

    def name(self, nm, row, rows):
        f = self.onto.field(self.entity, nm)
        if f is None:
            raise InterpError(f"`{nm}` is not a field of `{self.entity}`")
        return self.parent.field_value(row, self.alias, f)
