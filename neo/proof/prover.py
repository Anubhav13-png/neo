"""The proof service: runs a validated model against the pinned snapshot and proves the answer.

Z3 never sees raw rows at lake scale. Per form:
  value / table   the lake computes exact aggregates (integer SQL); Z3 derives each answer from them and
                  proves `answer != v` is unsat
  extremum        candidates from the lake; a verification query proves no row beats the answer
  set             members and a count; the count is re-derived independently
  check (rows)    a counterexample query: none found = holds on this snapshot
  check (global), feasibility, optimisation
                  Z3 over parameters and exact aggregates; constraints over every row (all_rows / count <= k
                  with variables) are solved directly for small universes, and by counterexample-guided
                  refinement for large ones: Z3 solves on a working set of rows, a lake query checks the
                  candidate against every row, violating rows are added, repeat. Optimality proved on a subset
                  holds for all rows (a subset only relaxes the constraints); feasibility is checked on all rows.
Every result is recomputed by an independent path (exact interpreter, or a float second engine for large
universes) and a disagreement turns the status into `mismatch`.
"""
from __future__ import annotations

import ast
import dataclasses
import time
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import z3

from ..compiler.floatsql import FloatCompiler
from ..compiler.sql import AggItem, CompileError, Compiler, R
from ..dsl import AGGREGATES
from ..dsl.parse import Expr, aggregate_nodes, parse, refs
from ..execution.interpreter import InterpError, Interpreter, TooLarge
from ..lake.engines import EngineError
from ..semantic.model import Scope, SemanticModel, Universe
from ..util import fmt_number, fraction_text
from .lean import Goals, conjuncts
from .lean import check as lean_check, summary as lean_summary
from .z3expr import Z3Error, Z3Translator, as_bool, from_z3, is_z3, real, z3val


class ProofError(RuntimeError):
    """The model is well-formed but cannot be executed or proved; the message goes back to the LLM."""


def fmt(v):
    if isinstance(v, Fraction):
        return fmt_number(v)
    if isinstance(v, dict):
        return {k: fmt(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [fmt(x) for x in v]
    return v


def same(a, b, exact: bool) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    if exact:
        return Fraction(a) == Fraction(b)
    fa, fb = float(a), float(b)
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))


def assignment_text(asg: dict) -> str:
    return ", ".join(f"{k} = {fmt(v)}" for k, v in asg.items())


def key_norm(v):
    return float(v) if isinstance(v, (Fraction, float, int)) and not isinstance(v, bool) else v


# ---------------------------------------------------------------------- independent verifiers
class ExactVerifier:
    method = "independent exact interpreter: Python fractions over the raw columns of the same snapshot"
    exact = True

    def __init__(self, p: "Prover"):
        self.interp = Interpreter(p.onto, p.m, p.engine, dict(p.params), p.scale, p.vcfg["exact_max_rows"],
                                  rules=p.rules)
        self.interp.load()
        self.rows = self.interp.eligible()
        own = {}
        for prm in p.m.params:
            if prm.has_agg and prm.name not in p.overrides:
                self.interp.params = {**p.params, **own}
                own[prm.name] = self.interp.whole(prm.expr, self.rows)
        self.params = {**p.params, **own}
        self.interp.params = self.params
        self.reason = ""

    def value(self, exprs: Dict[str, Expr]) -> dict:
        return {k: self.interp.whole(e, self.rows) for k, e in exprs.items()}

    def table(self, exprs: Dict[str, Expr]) -> dict:
        return {key_norm(g): {k: self.interp.whole(e, rs) for k, e in exprs.items()}
                for g, rs in self.interp.groups(self.rows).items()}

    def count(self, cond: Optional[Expr], want=True) -> int:
        return sum(1 for r in self.rows if cond is None or self.interp.row_value(cond, r) is want)

    def extremum(self, e: Expr, sense: str):
        vals = [v for v in (self.interp.row_value(e, r) for r in self.rows) if v is not None]
        return (max(vals) if sense == "max" else min(vals)) if vals else None


class FloatVerifier:
    method = "second engine path: an independent floating-point SQL compilation on the same snapshot (approximate)"
    exact = False

    def __init__(self, p: "Prover", reason: str):
        self.p = p
        self.fc = FloatCompiler(p.onto, p.m, p.engine, dict(p.params))
        own = {}
        for prm in p.m.params:
            if prm.has_agg and prm.name not in p.overrides:
                self.fc.params = {**p.params, **own}
                _, rows = p.sql("verifier: parameter", self.fc.query({"v": prm.expr}))
                own[prm.name] = self._num(rows[0][0])
        self.params = {**p.params, **own}
        self.fc.params = self.params
        self.reason = reason

    @staticmethod
    def _num(v):
        if v is None or isinstance(v, (bool, str)):
            return v
        return Fraction(repr(float(v)))

    def value(self, exprs):
        _, rows = self.p.sql("verifier: values", self.fc.query(exprs))
        return {k: self._num(v) for k, v in zip(exprs, rows[0])}

    def table(self, exprs):
        _, rows = self.p.sql("verifier: table", self.fc.query(exprs, grouped=True))
        return {key_norm(self._num(r[0])): {k: self._num(v) for k, v in zip(exprs, r[1:])} for r in rows}

    def count(self, cond, want=True):
        text = "count()" if cond is None else (f"count({cond.src})" if want else f"count(not ({cond.src}))")
        return int(self.value({"n": parse(text)})["n"])

    def extremum(self, e, sense):
        return self.value({"v": parse(f"{'max_of' if sense == 'max' else 'min_of'}({e.src})")})["v"]


# ---------------------------------------------------------------------- the prover
class Prover:
    def __init__(self, onto, model: SemanticModel, engine, cfg: dict, rules, overrides: Optional[dict] = None,
                 nested: bool = False):
        self.onto, self.m, self.engine, self.cfg, self.rules = onto, model, engine, cfg, rules
        self.overrides = overrides or {}
        self.nested = nested
        self.vcfg, self.pcfg = cfg["verification"], cfg["proof"]
        self.scale = self.vcfg.get("double_scale", 10)
        self.timeout = int(cfg["pipeline"].get("solver_timeout_seconds", 60)) * 1000
        self.queries: List[dict] = []
        self.z3_log: List[dict] = []         # what Z3 was asked, for people reading the run (never sent to the LLM)
        self.lean = Goals(cfg)               # theorems for Lean's kernel to check (lean.py)
        self.lean_values: List = []          # value facts, checked by Lean as one batch
        self.skipped: Dict[str, int] = {}
        self.inexact: set = set()
        self._verifier = None
        self._verifier_tried = False

    # ------------------------------------------------------------------ plumbing
    def sql(self, purpose: str, sql: str):
        t0 = time.time()
        try:
            cols, rows = self.engine.execute(sql)
        except EngineError as e:
            raise ProofError(f"the lake rejected the {purpose} query: {e}")
        self.queries.append({"purpose": purpose, "sql": sql, "ms": round((time.time() - t0) * 1000, 1),
                             "rows": len(rows)})
        return cols, rows

    def solver(self):
        s = z3.Solver()
        s.set("timeout", self.timeout)
        return s

    def finalize(self, items: List[AggItem], row, offset: int = 0, record: bool = True) -> Dict[int, object]:
        out, i = {}, offset
        for it in items:
            vals = list(row[i:i + len(it.columns)])
            i += len(it.columns)
            out[it.node_id] = it.finalize(vals)
            if record and it.skipped_index is not None and vals[it.skipped_index]:
                self.skipped[it.label] = self.skipped.get(it.label, 0) + int(vals[it.skipped_index])
            if not it.exact:
                self.inexact.add(it.label)
        return out

    def outer(self, e: Expr, agg: Dict[int, object], params: Optional[dict] = None):
        it = Interpreter(self.onto, self.m, self.engine, params if params is not None else self.params, self.scale, rules=self.rules,
                         agg_override=agg)
        return it.whole(e, [])

    def verifier(self):
        if not self._verifier_tried:
            self._verifier_tried = True
            try:
                self._verifier = ExactVerifier(self)
            except TooLarge as e:
                if self.vcfg.get("second_engine") == "duckdb_float":
                    self._verifier = FloatVerifier(self, f"{e}; the exact interpreter's limit is "
                                                         f"{self.vcfg['exact_max_rows']:,} rows")
        return self._verifier

    def self_check(self, compare) -> dict:
        v = self.verifier()
        if v is None:
            return {"method": "skipped", "matches": None, "note": "universe too large and no second engine configured"}
        mismatches = compare(v)
        return {"method": v.method, "exact": v.exact, "matches": not mismatches, "mismatches": mismatches[:20],
                "note": v.reason}

    def scope(self, zvars=None) -> Scope:
        return Scope(self.onto, self.m.universe, self.m.defines, self.params, zvars or {})

    def display_fields(self, entity: Optional[str] = None) -> list:
        ent = self.onto.entities[entity or self.m.universe.entity]
        out = []
        for name in [ent.key, getattr(ent, "extra", {}).get("label"), ent.time_field]:
            f = self.onto.field(ent.id, name) if name else None
            if f and f not in out:
                out.append(f)
        return [("", f) for f in out]

    def select_fields(self, c: Compiler, fields) -> Tuple[List[str], callable]:
        parts, kinds = [], []
        for alias, f in fields:
            r = c.field_r(alias, f)
            if r.kind == "num":
                parts += [r.sql, r.den]
            else:
                parts.append(r.sql)
            kinds.append((f.name if alias == "" else f"{alias}.{f.name}", r.kind))

        def decode(row, offset=0):
            rec, i = {}, offset
            for name, kind in kinds:
                if kind == "num":
                    n, d = row[i], row[i + 1]
                    rec[name] = None if n is None or d is None else Fraction(int(n), int(d))
                    i += 2
                else:
                    rec[name] = row[i]
                    i += 1
            return rec, i
        return parts, decode

    def sample(self, n: int = 8) -> List[dict]:
        used = [(a, f) for a, f in self.m.used_fields][:5]
        fields = self.display_fields() + [x for x in used if x not in self.display_fields()]
        parts, decode = self.select_fields(self.c, fields)
        _, rows = self.sql("evidence sample", f"SELECT {', '.join(parts)} FROM {self.c.from_clause()} "
                                              f"WHERE {self.c.where_clause()} LIMIT {n}")
        return [fmt(decode(r)[0]) for r in rows]

    # ------------------------------------------------------------------ entry
    def run(self) -> dict:
        t0 = time.time()
        try:
            self.params = self.parameters()
            self.c = Compiler(self.onto, self.m, self.engine, self.params, double_scale=self.scale)
            self.universe_rows, self.eligible_rows = self.counts()
            proof = getattr(self, f"form_{self.m.form}")()
            if self.m.backtest and not self.nested:
                proof["backtest"] = self.backtest()
            if self.m.sensitivity and not self.nested:
                proof["sensitivity"] = [self.sensitivity(s) for s in self.m.sensitivity]
        except (CompileError, InterpError, Z3Error) as e:
            raise ProofError(str(e))
        sc = proof.get("self_check") or {}
        if sc.get("matches") is False:
            proof["status_before_check"] = proof["status"]
            proof["status"] = "mismatch"
        proof.update({
            "form": self.m.form, "interpretation": self.m.interpretation,
            "parameters": {k: fmt(v) for k, v in self.params.items()},
            "stated_parameters": self.m.notes.get("stated_parameters", []),
            "eligible_rows": self.eligible_rows, "universe_rows": self.universe_rows,
            "skipped_missing": dict(self.skipped), "queries": self.queries,
            "exactness": {"exact": not self.inexact, "inexact": sorted(self.inexact)},
            "seconds": round(time.time() - t0, 2)})
        proof.setdefault("explain", {})["z3"] = self.z3_log[:20]
        if not self.nested:
            self.lean_run(proof)
        return proof

    def lean_run(self, proof: dict):
        form = self.m.form
        key = "check" if form == "check" and self.m.target.get("scope") != "global" else \
            {"value": "value", "table": "table", "extremum": "extremum", "set": "set"}.get(form)
        if key:
            self.lean.cite(key)
        if self.lean_values:
            self.lean.compute("values", "each value is what the lake's exact aggregates give", self.lean_values)
        proof["lean"] = lean_check(self.lean, self.cfg, f"neo run check, form `{form}`: {self.m.interpretation}")
        if proof["lean"]["status"] != "not_checked":
            proof.setdefault("verification", []).append(lean_summary(proof["lean"]))

    def parameters(self) -> dict:
        vals: Dict[str, object] = {}
        for p in self.m.params:
            if p.name in self.overrides:
                vals[p.name] = Fraction(repr(self.overrides[p.name])) if isinstance(self.overrides[p.name], float) \
                    else Fraction(self.overrides[p.name])
                continue
            if not p.has_agg:
                vals[p.name] = Interpreter(self.onto, self.m, self.engine, vals, self.scale,
                                           rules=self.rules).whole(p.expr, [])
                continue
            c = Compiler(self.onto, self.m, self.engine, vals, double_scale=self.scale)
            try:
                items = [c.aggregate(n) for n in aggregate_nodes(p.expr, AGGREGATES)]
                _, rows = self.sql(f"parameter {p.name}", c.aggregate_query(items))
            except KeyError as e:
                raise ProofError(f"parameter `{p.name}` needs {e} first: list parameters in dependency order")
            vals[p.name] = self.outer(p.expr, self.finalize(items, rows[0]), vals)
        return vals

    def counts(self) -> Tuple[int, int]:
        rows_cond = self.c.as_bool(self.c.expr(self.m.rows)) if self.m.rows is not None else "TRUE"
        sql = (f"SELECT COUNT(*), {self.c.d.count_if(f'COALESCE({rows_cond}, FALSE)')} FROM {self.c.from_clause()} "
               f"WHERE {self.c.where_clause(include_rows=False)}")
        _, rows = self.sql("universe and eligible row counts", sql)
        return int(rows[0][0]), int(rows[0][1] or 0)

    # ------------------------------------------------------------------ value / table
    def z3_value(self, e: Expr, agg: Dict[int, object], params=None) -> dict:
        tr = Z3Translator(self.scope(), self.m.defines, params if params is not None else self.params, {}, agg, fold=False)
        term = tr.term(e.body)
        if term is None:
            return {"value": None, "status": "undefined", "unique": None}
        if isinstance(term, str):
            return {"value": term, "status": "proved", "unique": True}
        is_bool = isinstance(term, bool) or (is_z3(term) and z3.is_bool(term))
        ans = z3.Bool("ans") if is_bool else z3.Real("ans")
        s = self.solver()
        s.add(ans == (z3val(term) if is_bool else real(term)))
        if s.check() != z3.sat:
            raise ProofError(f"Z3 could not evaluate `{e.src}`")
        found = from_z3(s.model().eval(ans, model_completion=True))
        s.add(ans != z3val(found))
        unique = s.check() == z3.unsat
        smt = "\n".join(l for l in s.sexpr().splitlines() if not l.startswith("(model-add"))
        if unique:
            self.lean_values.append(z3val(term) == z3val(found) if is_bool else real(term) == real(found))
        self.z3_log.append({"what": e.src, "smt": smt[:4000], "value": fmt(found),
                            "result": "unsat: no other value is possible" if unique else "sat: not unique"})
        return {"value": found, "status": "proved" if unique else "not unique", "unique": unique}

    def _target_aggs(self, exprs: List[Expr]) -> List[AggItem]:
        return [self.c.aggregate(n) for e in exprs for n in aggregate_nodes(e, AGGREGATES)]

    def form_value(self) -> dict:
        cols: Dict[str, Expr] = self.m.target["columns"]
        items = self._target_aggs(list(cols.values()))
        agg = {}
        if items:
            _, rows = self.sql("target aggregates", self.c.aggregate_query(items))
            agg = self.finalize(items, rows[0])
        answer, exact_text, all_unique, z3_agrees = {}, {}, True, True
        for k, e in cols.items():
            v = self.outer(e, agg)
            zp = self.z3_value(e, agg)
            all_unique &= zp["unique"] is not False
            z3_agrees &= same(zp["value"], v, True)
            answer[k] = v
            if isinstance(v, Fraction) and v.denominator != 1:
                exact_text[k] = fraction_text(v)
        status = "undefined" if all(v is None for v in answer.values()) else \
            ("proved" if all_unique and z3_agrees else "failed")

        def compare(ver):
            got = ver.value(cols)
            return [{"value": k, "primary": fmt(answer[k]), "independent": fmt(got[k])}
                    for k in cols if not same(answer[k], got[k], ver.exact)]
        scalar = list(cols) == ["value"] and "expr" in (self.m.raw.get("target") or {})
        return {"status": status, "answer": fmt(answer["value"]) if scalar else fmt(answer),
                "exact_values": exact_text,
                "answer_summary": "; ".join(f"{k} = {fmt(v)}" for k, v in answer.items()),
                "verification": [
                    {"step": f"{len(items)} aggregate(s) over {self.eligible_rows:,} eligible rows",
                     "method": "computed inside the lake with exact integer SQL (numerator / denominator)",
                     "result": "done"},
                    {"step": f"each of {len(cols)} value(s) from those aggregates",
                     "method": "Z3 derives the value, then proves value != v is unsat",
                     "result": "proved" if all_unique and z3_agrees else "failed", "detail": exact_text or None}],
                "self_check": self.self_check(compare), "evidence": {"sample": self.sample(), "row_data": True}}

    def group_key(self, g: R, raw):
        if raw is None:
            return None
        return Fraction(int(raw), g.den_const) if g.kind == "num" else raw

    def form_table(self) -> dict:
        cols: Dict[str, Expr] = self.m.target["columns"]
        g = self.c.group_expr()
        items = self._target_aggs(list(cols.values()))
        _, rows = self.sql("grouped aggregates", self.c.aggregate_query(items, g))
        if len(rows) > 500:
            raise ProofError(f"group_by makes {len(rows)} groups; group more coarsely (at most 500)")
        table, cells_ok, cells = [], True, 0
        for row in rows:
            key = self.group_key(g, row[0])
            agg = self.finalize(items, row, offset=1)
            rec = {"group": key}
            for k, e in cols.items():
                v = self.outer(e, agg)
                zp = self.z3_value(e, agg)
                cells_ok &= zp["unique"] is not False and same(zp["value"], v, True)
                cells += 1
                rec[k] = v
            table.append(rec)

        def compare(ver):
            got = ver.table(cols)
            bad = []
            for rec in table:
                other = got.get(key_norm(rec["group"]))
                if other is None:
                    bad.append({"group": fmt(rec["group"]), "problem": "missing in the independent result"})
                    continue
                for k in cols:
                    if not same(rec[k], other[k], ver.exact):
                        bad.append({"group": fmt(rec["group"]), "column": k, "primary": fmt(rec[k]),
                                    "independent": fmt(other[k])})
            if len(got) != len(table):
                bad.append({"problem": f"{len(table)} groups vs {len(got)} independently"})
            return bad
        sort_by = self.m.target.get("sort_by")
        if sort_by and table and sort_by in table[0]:
            table.sort(key=lambda r: (r[sort_by] is None, r[sort_by] if r[sort_by] is not None else 0),
                       reverse=self.m.target.get("descending", True))
        else:
            table.sort(key=lambda r: (r["group"] is None, r["group"] if r["group"] is not None else 0))
        limit = int(self.m.target.get("limit", 500))
        return {"status": "proved" if cells_ok else "failed", "answer": fmt(table[:limit]),
                "answer_summary": f"{len(table)} groups by {self.m.group_by.src}",
                "verification": [
                    {"step": f"{len(items)} aggregate(s) per group over {self.eligible_rows:,} eligible rows",
                     "method": "exact integer SQL with GROUP BY inside the lake", "result": "done"},
                    {"step": f"each of {cells} cells", "method": "Z3 derives the cell, then proves cell != v is unsat",
                     "result": "proved" if cells_ok else "failed"}],
                "self_check": self.self_check(compare), "evidence": {"groups": len(table)}}

    # ------------------------------------------------------------------ extremum
    def form_extremum(self) -> dict:
        e: Expr = self.m.target["expr"]
        sense, top = self.m.target["sense"], self.m.target.get("top", 1)
        if self.m.group_by is not None:
            return self._extremum_groups(e, sense, top)
        r = self.c.expr(e)
        not_null = f"(({r.sql}) IS NOT NULL)" + (f" AND (({r.den}) IS NOT NULL)" if r.kind == "num" else "")
        order = r.sql if r.kind == "text" else f"({self.c.d.to_double(r.sql)} / {self.c.d.to_double(r.den)})"
        disp, decode = self.select_fields(self.c, self.display_fields())
        val_cols = [r.sql, r.den] if r.kind == "num" else [r.sql]
        limit = top + int(self.pcfg.get("extremum_candidates", 64))
        for attempt in range(2):
            sql = (f"SELECT {', '.join(disp + val_cols)} FROM {self.c.from_clause()} "
                   f"WHERE {self.c.where_clause(not_null)} ORDER BY {order} {'DESC' if sense == 'max' else 'ASC'} "
                   f"LIMIT {limit}")
            _, rows = self.sql("extremum candidates", sql)
            cands = []
            for row in rows:
                rec, i = decode(row)
                val = Fraction(int(row[i]), int(row[i + 1])) if r.kind == "num" else row[i]
                cands.append((val, rec))
            if not cands:
                return {"status": "undefined", "answer": None, "answer_summary": "no eligible row has a value",
                        "verification": [], "self_check": {"matches": None}, "evidence": {}}
            cands.sort(key=lambda c: c[0], reverse=sense == "max")
            ranking, verified = [], True
            better_op = ast.Gt() if sense == "max" else ast.Lt()
            for rank, (val, rec) in enumerate(cands[:top]):
                lit = self.c.literal(val)
                cond = self.c.compare_pair(better_op, r, lit)
                _, cnt = self.sql(f"verification: rows beating rank {rank + 1}",
                                  f"SELECT COUNT(*) FROM {self.c.from_clause()} WHERE {self.c.where_clause(cond)}")
                beating = int(cnt[0][0])
                expected = sum(1 for v, _ in cands if (v > val if sense == "max" else v < val))
                verified &= beating == expected
                ranking.append({"rank": rank + 1, "value": val, **rec, "rows_beating_it": beating})
            if verified or len(rows) < limit:
                break
            limit *= 8
        pick = z3.Int("pick")
        opt = z3.Optimize()
        opt.set("timeout", self.timeout)
        opt.add(pick >= 0, pick < len(cands))
        nums = [c[0] if r.kind == "num" else Fraction(k) for k, c in enumerate(sorted(cands, key=lambda c: c[0]))]
        if r.kind == "num":
            obj = real(nums[-1])
            for k in range(len(nums) - 2, -1, -1):
                obj = z3.If(pick == k, real(nums[k]), obj)
            (opt.maximize if sense == "max" else opt.minimize)(obj)
            opt.check()
            z3_best = from_z3(opt.model().eval(obj, model_completion=True))
            z3_ok = z3_best == cands[0][0]
        else:
            z3_ok = True
        _, miss = self.sql("rows without a value",
                           f"SELECT COUNT(*) FROM {self.c.from_clause()} WHERE {self.c.where_clause(f'NOT ({not_null})')}")
        best = cands[0][0]

        def compare(ver):
            got = ver.extremum(e, sense)
            return [] if same(best, got, ver.exact) else [{"primary": fmt(best), "independent": fmt(got)}]
        return {"status": "proved" if verified and z3_ok else "failed",
                "answer": fmt(ranking if top > 1 else ranking[0]),
                "answer_summary": f"{sense} of {e.src} = {fmt(best)}",
                "verification": [
                    {"step": f"{len(cands)} candidates from the lake, ranked exactly", "method": "ORDER BY + exact fractions",
                     "result": "done"},
                    {"step": f"no row outside the ranking beats ranks 1..{min(top, len(cands))}",
                     "method": "verification query per rank: COUNT(*) of eligible rows strictly better (exact comparison)",
                     "result": "proved" if verified else "failed"},
                    {"step": "best candidate", "method": "Z3 Optimize over the candidates",
                     "result": "proved" if z3_ok else "failed"}],
                "self_check": self.self_check(compare),
                "evidence": {"missing_value": int(miss[0][0]), "row_data": True}}

    def _extremum_groups(self, e: Expr, sense: str, top: int) -> dict:
        g = self.c.group_expr()
        items = self._target_aggs([e])
        size = AggItem(-1, "count()", ["COUNT(*)"], lambda v: Fraction(v[0]))
        _, rows = self.sql("grouped candidates", self.c.aggregate_query(items + [size], g))
        cands = []
        for row in rows:
            agg = self.finalize(items + [size], row, offset=1)
            v = self.outer(e, agg)
            if v is not None:
                cands.append((self.group_key(g, row[0]), v, int(agg[-1])))
        if not cands:
            return {"status": "undefined", "answer": None, "answer_summary": "no group has a value",
                    "verification": [], "self_check": {"matches": None}, "evidence": {}}
        text = isinstance(cands[0][1], str)
        order = sorted({c[1] for c in cands})
        nums = [Fraction(order.index(c[1])) if text else c[1] for c in cands]
        pick = z3.Int("pick")
        opt = z3.Optimize()
        opt.set("timeout", self.timeout)
        opt.add(pick >= 0, pick < len(cands))
        obj = real(nums[-1])
        for k in range(len(nums) - 2, -1, -1):
            obj = z3.If(pick == k, real(nums[k]), obj)
        (opt.maximize if sense == "max" else opt.minimize)(obj)
        if opt.check() != z3.sat:
            raise ProofError("Z3 could not pick the extremum")
        k_best = opt.model().eval(pick).as_long()
        best = nums[k_best]
        s = self.solver()
        s.add(z3.Or(*[z3.BoolVal(v > best if sense == "max" else v < best) for v in nums]))
        optimal = s.check() == z3.unsat
        ranked = sorted(range(len(cands)), key=lambda k: nums[k], reverse=sense == "max")
        ranking = [{"rank": i + 1, "group": cands[k][0], "value": cands[k][1], "rows": cands[k][2]}
                   for i, k in enumerate(ranked[:top])]

        def compare(ver):
            got = ver.table({"value": e})
            vals = [x["value"] for x in got.values() if x["value"] is not None]
            if not vals:
                return [{"problem": "no group has a value independently"}]
            other = max(vals) if sense == "max" else min(vals)
            return [] if same(cands[k_best][1], other, ver.exact) else \
                [{"primary": fmt(cands[k_best][1]), "independent": fmt(other)}]
        return {"status": "proved" if optimal else "failed", "answer": fmt(ranking if top > 1 else ranking[0]),
                "answer_summary": f"{sense} of {e.src} by {self.m.group_by.src} = {fmt(cands[k_best][1])} "
                                  f"(group {fmt(cands[k_best][0])})",
                "verification": [{"step": f"{sense}imise over {len(cands)} groups", "method": "Z3 Optimize", "result": "sat"},
                                 {"step": "optimality", "method": "Z3: some group strictly better",
                                  "result": "proved (unsat)" if optimal else "failed"}],
                "self_check": self.self_check(compare), "evidence": {"groups": len(cands)}}

    # ------------------------------------------------------------------ set
    def form_set(self) -> dict:
        where: Optional[Expr] = self.m.target.get("where")
        limit = int(self.m.target.get("limit", 50))
        if self.m.group_by is not None:
            return self._set_groups(where, limit)
        cond = self.c.as_bool(self.c.expr(where)) if where is not None else None
        _, cnt = self.sql("set size", f"SELECT COUNT(*) FROM {self.c.from_clause()} WHERE {self.c.where_clause(cond)}")
        count = int(cnt[0][0])
        sel = [self.scope().field(s) for s in self.m.target.get("select") or [] if self.scope().field(s)]
        fields = self.display_fields() + [x for x in sel if x not in self.display_fields()]
        parts, decode = self.select_fields(self.c, fields)
        key = self.display_fields()[0] if self.display_fields() else None
        order = f" ORDER BY {self.c.field_r(*key).sql}" if key else ""
        _, rows = self.sql("set members", f"SELECT {', '.join(parts)} FROM {self.c.from_clause()} "
                                          f"WHERE {self.c.where_clause(cond)}{order} LIMIT {limit}")
        members = [decode(r)[0] for r in rows]

        def compare(ver):
            got = ver.count(where)
            return [] if got == count else [{"primary": count, "independent": got}]
        return {"status": "proved", "answer": {"count": count, "members": fmt(members), "truncated": count > limit},
                "answer_summary": f"{count} rows satisfy the conditions",
                "verification": [{"step": "membership", "method": "the lake filters every eligible row exactly",
                                  "result": f"{count} members"},
                                 {"step": "exactness", "method": "the count is re-derived by the independent path",
                                  "result": "see self-check"}],
                "self_check": self.self_check(compare), "evidence": {"row_data": True}}

    def _set_groups(self, where, limit) -> dict:
        g = self.c.group_expr()
        items = self._target_aggs([where]) if where is not None else []
        size = AggItem(-1, "count()", ["COUNT(*)"], lambda v: Fraction(v[0]))
        _, rows = self.sql("grouped set", self.c.aggregate_query(items + [size], g))
        members = []
        for row in rows:
            agg = self.finalize(items + [size], row, offset=1)
            if where is None or self.outer(where, agg) is True:
                members.append({"group": self.group_key(g, row[0]), "rows": int(agg[-1])})

        def compare(ver):
            if where is None:
                return []
            got = {k for k, v in ver.table({"in": where}).items() if v["in"] is True}
            mine = {key_norm(m["group"]) for m in members}
            return [] if got == mine else [{"primary": len(mine), "independent": len(got)}]
        return {"status": "proved", "answer": {"count": len(members), "members": fmt(members[:limit]),
                                               "truncated": len(members) > limit},
                "answer_summary": f"{len(members)} groups satisfy the conditions",
                "verification": [{"step": "group membership", "method": "exact aggregates per group", "result": "done"}],
                "self_check": self.self_check(compare), "evidence": {}}

    # ------------------------------------------------------------------ check (rows)
    def form_check(self) -> dict:
        if self.m.target["scope"] == "global":
            return self.logic("check")
        claim: Expr = self.m.target["claim"]
        c = self.c.as_bool(self.c.expr(claim))
        fail, unknown = f"(NOT COALESCE({c}, TRUE))", f"(({c}) IS NULL)"
        _, cnt = self.sql("claim counts", f"SELECT COUNT(*), {self.c.d.count_if(fail)}, {self.c.d.count_if(unknown)} "
                                          f"FROM {self.c.from_clause()} WHERE {self.c.where_clause()}")
        total, failing, unk = int(cnt[0][0]), int(cnt[0][1] or 0), int(cnt[0][2] or 0)
        used = [x for x in self.m.used_fields][:4]
        parts, decode = self.select_fields(self.c, self.display_fields() + [u for u in used if u not in self.display_fields()])
        _, rows = self.sql("counterexamples", f"SELECT {', '.join(parts)} FROM {self.c.from_clause()} "
                                              f"WHERE {self.c.where_clause(fail)} LIMIT 10")
        counter = [fmt(decode(r)[0]) for r in rows]

        def compare(ver):
            f2 = ver.count(claim, want=False)
            return [] if f2 == failing else [{"primary_failing": failing, "independent_failing": f2}]
        holds = failing == 0
        return {"status": "proved" if holds else "refuted",
                "answer": "holds" if holds else f"does not hold: {failing} of {total - unk} rows break it",
                "answer_summary": f"`{claim.src}` {'holds on all' if holds else 'fails on'} "
                                  f"{total - unk if holds else failing} rows",
                "verification": [{"step": "search every eligible row for a counterexample",
                                  "method": "verification query: COUNT(*) WHERE NOT claim",
                                  "result": "none (proved on this snapshot)" if holds else f"{failing} found"},
                                 {"step": "rows where the claim cannot be decided (missing values)", "method": "count",
                                  "result": unk}],
                "self_check": self.self_check(compare), "evidence": {"counterexamples": counter, "row_data": True}}

    def form_feasibility(self) -> dict:
        return self.logic("feasibility")

    def form_optimisation(self) -> dict:
        return self.logic("optimisation")

    def form_not_expressible(self) -> dict:
        return {"status": "not_expressible", "answer": None, "answer_summary": self.m.interpretation,
                "verification": [], "self_check": {"matches": None}, "evidence": {}}

    # ------------------------------------------------------------------ logic: check global, feasibility, optimisation
    def _var_defines(self) -> set:
        var_names = {v.name for v in self.m.variables}
        out, changed = set(), True
        while changed:
            changed = False
            for name, e in self.m.defines.items():
                if name not in out and refs(e)[0] & (var_names | out):
                    out.add(name)
                    changed = True
        return out

    def _fields_of(self, nodes) -> list:
        """Fields (alias, Field) the symbolic aggregates read, following defines."""
        sc, out, seen = self.scope(), [], set()

        def walk(n):
            for nm in refs(Expr(n, ast.unparse(n)))[0]:
                k = sc.kind(nm)
                if k == "define" and nm not in seen:
                    seen.add(nm)
                    walk(self.m.defines[nm].body)
                elif k == "field":
                    af = sc.field(nm)
                    if af not in out:
                        out.append(af)
        for n in nodes:
            for a in n.args:
                walk(a)
        return out

    def logic(self, kind: str) -> dict:
        m = self.m
        zvars = {v.name: {"Real": z3.Real, "Int": z3.Int, "Bool": z3.Bool}[v.sort](v.name) for v in m.variables}
        symbolic_names = set(zvars) | self._var_defines()
        named: List[Tuple[str, Expr]] = [(f"domain:{v.name}", v.domain) for v in m.variables if v.domain is not None]
        named += [(c.name, c.expr) for c in m.constraints] + [(a.name, a.expr) for a in m.assumptions]
        extra = m.target.get("objective") if kind == "optimisation" else (m.target.get("claim") if kind == "check" else None)
        sym_nodes, plain_nodes = [], []
        for e in [x for _, x in named] + ([extra] if extra is not None else []):
            for n in aggregate_nodes(e, AGGREGATES):
                (sym_nodes if refs(Expr(n, ast.unparse(n)))[0] & symbolic_names else plain_nodes).append(n)
        agg = {}
        if plain_nodes:
            items = [self.c.aggregate(n) for n in plain_nodes]
            _, rows = self.sql("aggregates used by the constraints", self.c.aggregate_query(items))
            agg = self.finalize(items, rows[0])
        mode, fields = "none", []
        if sym_nodes:
            fields = self._fields_of(sym_nodes)
            mode = "full" if self.eligible_rows <= self.pcfg.get("symbolic_max_rows", 5000) else "cegar"
            if mode == "cegar":
                self._check_cegar_shapes(named, extra, symbolic_names)
        ctx = _Logic(self, zvars, agg, named, fields, mode, symbolic_names)
        if kind == "feasibility":
            out = ctx.feasibility()
        elif kind == "optimisation":
            out = ctx.optimisation(extra, m.target["sense"])
        else:
            out = ctx.check_global(extra)
        out["explain"] = ctx.explain(agg)
        return out

    def _check_cegar_shapes(self, named, extra, symbolic_names):
        def symbolic(n):
            return bool(refs(Expr(n, ast.unparse(n)))[0] & symbolic_names)
        for name, e in named:
            body = e.body
            allowed = set()
            if isinstance(body, ast.Call) and body.func.id == "all_rows":
                allowed.add(id(body))
            if isinstance(body, ast.Compare) and len(body.ops) == 1:
                l, op, r = body.left, body.ops[0], body.comparators[0]
                if isinstance(l, ast.Call) and l.func.id == "count" and isinstance(op, (ast.LtE, ast.Lt)):
                    allowed.add(id(l))
                if isinstance(r, ast.Call) and r.func.id == "count" and isinstance(op, (ast.GtE, ast.Gt)):
                    allowed.add(id(r))
            for n in aggregate_nodes(e, AGGREGATES):
                if symbolic(n) and id(n) not in allowed:
                    raise ProofError(
                        f"`{name}`: with {self.eligible_rows:,} eligible rows (more than "
                        f"{self.pcfg.get('symbolic_max_rows', 5000):,}), an aggregate that depends on a variable must "
                        f"be a whole constraint `all_rows(...)` or `count(...) <= k`, so it can be solved by refinement.")
        if extra is not None and any(symbolic(n) for n in aggregate_nodes(extra, AGGREGATES)):
            raise ProofError("for large universes the objective or claim cannot contain an aggregate that depends on "
                             "a variable; move it into a constraint")
        if self.m.universe.fanned:
            raise ProofError("refinement over a universe with a one-to-many join is not supported; aggregate at the "
                             "parent grain")

    # ------------------------------------------------------------------ backtest and sensitivity
    def backtest(self) -> dict:
        bt = self.m.backtest
        ent = bt["entity"]
        pol = {"": self.m.policies[""]} if ent == self.m.universe.entity and "" in self.m.policies else {}
        bm = dataclasses.replace(self.m, universe=Universe(ent), rows=bt["rows"], policies=pol, defines={},
                                 group_by=None)
        bc = Compiler(self.onto, bm, self.engine, self.params, double_scale=self.scale)
        exprs = list(bt["bind"].items()) + [(f"param:{k}", v) for k, v in bt["parameters"].items()]
        parts, kinds = [], []
        for name, e in exprs:
            r = bc.expr(e)
            parts += [r.sql, r.den] if r.kind == "num" else [r.sql]
            kinds.append((name, r.kind))
        dparts, decode = self.select_fields(bc, self.display_fields(ent))
        _, rows = self.sql("backtest cases", f"SELECT {', '.join(dparts + parts)} FROM {bc.from_clause()} "
                                             f"WHERE {bc.where_clause()} LIMIT {bt['limit']}")
        zvars = {v.name: {"Real": z3.Real, "Int": z3.Int, "Bool": z3.Bool}[v.sort](v.name) for v in self.m.variables}
        records, contradictions, skipped = [], [], 0
        for row in rows:
            rec, i = decode(row)
            vals = {}
            for name, kind in kinds:
                if kind == "num":
                    vals[name] = None if row[i] is None or row[i + 1] is None else Fraction(int(row[i]), int(row[i + 1]))
                    i += 2
                else:
                    vals[name] = row[i]
                    i += 1
            if any(v is None for v in vals.values()):
                skipped += 1
                continue
            params = {**self.params, **{k[6:]: v for k, v in vals.items() if k.startswith("param:")}}
            bound = {k: v for k, v in vals.items() if not k.startswith("param:")}
            zv = {**zvars, **bound}
            tr = Z3Translator(Scope(self.onto, self.m.universe, self.m.defines, params, zv), self.m.defines, params,
                              zv, {}, fold=True)
            s = self.solver()
            s.set("unsat_core", True)
            for v in self.m.variables:
                if v.domain is not None and v.name not in bound:
                    s.add(as_bool(tr.term(v.domain.body)))
            for c in self.m.constraints:
                if aggregate_nodes(c.expr, AGGREGATES):
                    raise ProofError("backtest cannot evaluate constraints that aggregate over rows")
                s.assert_and_track(as_bool(tr.term(c.expr.body)), c.name)
            allowed = s.check() == z3.sat
            out = {**fmt(rec), "allowed": allowed, **{k: fmt(v) for k, v in vals.items()}}
            if not allowed:
                out["blocked_by"] = sorted(str(x) for x in s.unsat_core())
                contradictions.append(out)
            records.append(out)
        return {"cases": len(records), "skipped_missing_data": skipped, "contradictions": contradictions[:20],
                "passed": not contradictions, "rows": records[:30],
                "meaning": "each case really happened, so the rules should allow it; a contradiction means the rules "
                           "or the mapping are wrong"}

    def sensitivity(self, spec: dict) -> dict:
        runs = []
        for value in spec["values"]:
            sub = Prover(self.onto, self.m, self.engine, self.cfg, self.rules,
                         overrides={**self.overrides, spec["parameter"]: value}, nested=True)
            p = sub.run()
            ans = p.get("answer")
            runs.append({"value": value, "status": p.get("status"),
                         "answer": p.get("answer_summary") if isinstance(ans, list) else ans})
        return {"parameter": spec["parameter"], "runs": runs}


class _Logic:
    """Z3 over constraints, with every-row aggregates solved directly or by counterexample-guided refinement."""

    def __init__(self, p: Prover, zvars, agg, named, fields, mode, symbolic_names):
        self.p, self.zvars, self.agg, self.named, self.fields, self.mode = p, zvars, agg, named, fields, mode
        self.symbolic_names = symbolic_names
        self.rounds = 0
        self.rows_used = 0
        self.key = None
        self.trace: List[dict] = []          # one entry per solve: candidate, violating rows found, rows added
        if mode == "full":
            self.W = self.fetch(None, None)
        elif mode == "cegar":
            dfs = p.display_fields()
            if not dfs or not dfs[0][1].column:
                raise ProofError("refinement needs the universe entity to have a column key")
            self.key = dfs[0]
            self.W = self.fetch(None, p.pcfg.get("cegar_seed_rows", 256), order=True)
        else:
            self.W = None

    # rows -------------------------------------------------------------
    def fetch(self, extra_cond: Optional[str], limit: Optional[int], order: bool = False, exclude=None) -> list:
        p = self.p
        fields = list(self.fields) + ([self.key] if self.key and self.key not in self.fields else [])
        parts, decode = p.select_fields(p.c, fields)
        cond = extra_cond
        if exclude:
            kcol = p.c.field_r(*self.key).sql
            keys = ", ".join(str(int(k)) if isinstance(k, Fraction) else p.c.d.lit_str(str(k)) for k in exclude)
            cond = f"({cond}) AND {kcol} NOT IN ({keys})" if cond else f"{kcol} NOT IN ({keys})"
        sql = f"SELECT {', '.join(parts) or '1'} FROM {p.c.from_clause()} WHERE {p.c.where_clause(cond)}"
        if order and self.key:
            sql += f" ORDER BY {p.c.field_r(*self.key).sql}"
        if limit:
            sql += f" LIMIT {limit}"
        _, rows = p.sql("rows for the solver" if extra_cond is None else "violating rows", sql)
        out = []
        for r in rows:
            rec, _ = decode(r)
            out.append({(a, f.name): rec[f.name if a == "" else f"{a}.{f.name}"] for a, f in fields})
        self.rows_used = max(self.rows_used, len(out))
        return out

    def keyval(self, row):
        return row[(self.key[0], self.key[1].name)]

    def translator(self, zvars=None) -> Z3Translator:
        zv = zvars if zvars is not None else self.zvars
        return Z3Translator(self.p.scope(zv), self.p.m.defines, self.p.params, zv, self.agg, fold=True,
                            field_eval=lambda row, alias, f: row[(alias, f.name)])

    def hard(self, rows, skip=()) -> List[Tuple[str, object]]:
        tr = self.translator()
        return [(name, as_bool(tr.term(e.body, None, rows))) for name, e in self.named if name not in skip]

    # verification against every row --------------------------------------
    def violations(self, asg: dict, skip=()) -> Tuple[int, list]:
        """Rows (not yet in the working set) that the assignment violates, from a lake query over every row."""
        if self.mode != "cegar":
            return 0, []
        p = self.p
        c2 = Compiler(p.onto, p.m, p.engine, p.params, variables=asg, double_scale=p.scale)
        tr = self.translator(zvars={k: v for k, v in asg.items()})
        total, new_rows = 0, []
        have = {self.keyval(r) for r in self.W}
        for name, e in self.named:
            if name in skip:
                continue
            body = e.body
            if isinstance(body, ast.Call) and body.func.id == "all_rows" and self._symbolic(body):
                pc = c2.as_bool(c2.expr(Expr(body.args[0], ast.unparse(body.args[0]))))
                bad = f"(NOT COALESCE({pc}, FALSE))"
            elif isinstance(body, ast.Compare) and any(isinstance(x, ast.Call) and x.func.id == "count" and
                                                       self._symbolic(x) for x in (body.left, body.comparators[0])):
                cnt = body.left if isinstance(body.left, ast.Call) and body.left.func.id == "count" else body.comparators[0]
                kexpr = body.comparators[0] if cnt is body.left else body.left
                k = tr.term(kexpr)
                pc = c2.as_bool(c2.expr(Expr(cnt.args[0], ast.unparse(cnt.args[0])))) if cnt.args else "TRUE"
                _, n = p.sql(f"verification: {name}", f"SELECT {p.c.d.count_if(f'COALESCE({pc}, FALSE)')} "
                                                      f"FROM {p.c.from_clause()} WHERE {p.c.where_clause()}")
                n = int(n[0][0] or 0)
                op = body.ops[0]
                strict = isinstance(op, (ast.Lt, ast.Gt))
                if (n < k) if strict else (n <= k):
                    continue
                bad = f"COALESCE({pc}, FALSE)"
            else:
                continue
            _, cnt_rows = p.sql(f"verification: {name}", f"SELECT COUNT(*) FROM {p.c.from_clause()} "
                                                         f"WHERE {p.c.where_clause(bad)}")
            nbad = int(cnt_rows[0][0])
            total += nbad
            if nbad:
                new_rows += self.fetch(bad, p.pcfg.get("cegar_batch", 256), exclude=have)
        uniq = {}
        for r in new_rows:
            uniq.setdefault(self.keyval(r), r)
        return total, list(uniq.values())

    def _symbolic(self, n) -> bool:
        return bool(refs(Expr(n, ast.unparse(n)))[0] & self.symbolic_names)

    def assignment(self, model) -> dict:
        return {name: from_z3(model.eval(v, model_completion=True)) for name, v in self.zvars.items()}

    def cegar_check(self, extra_formulas, skip=()):
        """Loop until the result holds on every row. Returns ('sat', asg, model) or ('unsat', core, None)."""
        max_it = self.p.pcfg.get("cegar_max_iterations", 40)
        for it in range(max_it):
            self.rounds = max(self.rounds, it + 1)
            s = self.p.solver()
            s.set("unsat_core", True)
            for name, f in self.hard(self.W, skip):
                s.assert_and_track(f, name)
            for f in extra_formulas:
                s.add(f)
            r = s.check()
            if r == z3.unsat:
                return "unsat", sorted(str(c) for c in s.unsat_core()), None
            if r != z3.sat:
                raise ProofError(f"solver returned {r} ({s.reason_unknown()})")
            asg = self.assignment(s.model())
            size = len(self.W) if self.W is not None else 0
            nbad, new = self.violations(asg, skip)
            self.trace.append({"solve": "find values" + (f" without {', '.join(skip)}" if skip else ""),
                               "working_rows": size, "candidate": fmt(asg), "violating_rows": nbad, "added": len(new)})
            if not nbad:
                return "sat", asg, s.model()
            if not new:
                raise ProofError("refinement found violations but could not fetch the violating rows")
            self.W += new
        raise ProofError(f"refinement did not converge in {max_it} rounds")

    # forms ---------------------------------------------------------------
    def steps_mode(self) -> dict:
        if self.mode == "none":
            return {"method": "Z3 over parameters and exact aggregates from the lake"}
        if self.mode == "full":
            return {"method": f"Z3 over every eligible row ({len(self.W):,}) for aggregates that depend on variables"}
        return {"method": f"counterexample-guided refinement: Z3 on {len(self.W):,} working rows, every candidate "
                          f"checked against all {self.p.eligible_rows:,} eligible rows by lake queries "
                          f"({self.rounds} round(s))"}

    def witness_ok(self, model, rows) -> bool:
        return all(z3.is_true(model.eval(f, model_completion=True)) for _, f in self.hard(rows))

    def probes(self, core) -> list:
        droppable = [a.name for a in self.p.m.assumptions] + \
            [c for c in core if not c.startswith("domain:") and c not in {a.name for a in self.p.m.assumptions}]
        out = []
        for name in droppable:
            res, val, _ = self.cegar_check([], skip=(name,))
            out.append({"drop": name, "result": res, "witness": fmt(val) if res == "sat" else None})
        return out

    def explain(self, agg) -> dict:
        """How the constraints were handed to Z3: a few rows as concrete formulas, and the solve rounds."""
        out = {"mode": self.mode, "rounds": self.trace,
               "aggregates": {str(i): fmt(v) for i, v in list(agg.items())[:12]}}
        if self.W:
            sample = self.W[:3]
            s = z3.Solver()
            for name, f in self.hard(sample):
                s.add(f)
            out["sample_rows"] = [{(f"{a}.{name}" if a else name): fmt(v) for (a, name), v in row.items()}
                                  for row in sample]
            out["smt_sample"] = s.sexpr()[:6000]
        else:
            s = z3.Solver()
            for name, f in self.hard(None):
                s.add(f)
            out["smt_sample"] = s.sexpr()[:6000]
        return out

    # Lean goals -----------------------------------------------------------
    def facts(self) -> list:
        """Every working-row constraint, one formula per row (an `all_rows` split into its rows)."""
        return [c for _, f in self.hard(self.W) for c in conjuncts(f)]

    def core(self, goal) -> Optional[list]:
        """The few row formulas that already make `goal` impossible (Z3 unsat core, minimised)."""
        parts = self.facts()
        s = self.p.solver()
        s.set("core.minimize", True)
        for i, f in enumerate(parts):
            s.assert_and_track(f, f"k{i}")
        if goal is not None:
            s.add(goal)
        if s.check() != z3.unsat:
            return None
        return [parts[int(str(k)[1:])] for k in s.unsat_core()]

    def lean_witness(self, asg: dict, about: str, extra=()):
        facts = self.facts() + list(extra)
        self.p.lean.compute("witness", about, facts, env=asg)

    def feasibility(self) -> dict:
        res, val, model = self.cegar_check([])
        mode = self.steps_mode()
        if res == "sat":
            ok = self.witness_ok(model, self.W)
            self.p.lean.cite("feasible")
            self.lean_witness(val, f"the witness {assignment_text(val)} satisfies every constraint on the "
                                   f"{len(self.W) if self.W is not None else 0:,} working rows")
            return {"status": "feasible", "answer": "possible", "answer_summary": f"possible, e.g. {fmt(val)}",
                    "verification": [{"step": "all constraints and assumptions together", **mode, "result": "sat",
                                      "detail": fmt(val)},
                                     {"step": "witness re-checked against every constraint",
                                      "method": "model evaluation" + ("" if self.mode != "cegar" else
                                                                       " + lake verification over every row"),
                                      "result": "proved" if ok else "failed"}],
                    "self_check": {"method": "each constraint evaluated under the witness", "matches": ok},
                    "solver": {"witness": fmt(val), "mode": self.mode}, "evidence": {}}
        core = val
        self.p.lean.cite("infeasible")
        clash = self.core(None)
        if clash is not None:
            self.p.lean.symbolic("clash", f"these {len(clash)} row formulas cannot all hold, for any value of the "
                                          f"unknowns", clash, None)
        probes = self.probes(core)
        opened = [x["drop"] for x in probes if x["result"] == "sat"]
        return {"status": "infeasible", "answer": "impossible",
                "answer_summary": f"impossible: {', '.join(core)} cannot all hold",
                "verification": [{"step": "all constraints and assumptions together", **mode, "result": "unsat"},
                                 {"step": "minimal clashing set (unsat core)", "method": "Z3 assert_and_track",
                                  "result": "proved", "detail": core},
                                 {"step": "probes: drop one at a time", "method": "Z3 (with refinement when needed)",
                                  "result": "done", "detail": {x["drop"]: x["result"] for x in probes}}],
                "self_check": {"method": "dropping a core member must restore satisfiability",
                               "matches": any(x["result"] == "sat" for x in probes if x["drop"] in core) if core else None},
                "solver": {"unsat_core": core, "probes": probes, "routes_that_open": opened, "mode": self.mode},
                "evidence": {}}

    def optimisation(self, objective: Expr, sense: str) -> dict:
        p = self.p
        max_it = p.pcfg.get("cegar_max_iterations", 40)
        big = Fraction(repr(float(p.pcfg.get("unbounded_probe", 1e15))))
        for it in range(max_it):
            self.rounds = it + 1
            tr = self.translator()
            obj = real(tr.term(objective.body, None, self.W))
            opt = z3.Optimize()
            opt.set("timeout", p.timeout)
            for _, f in self.hard(self.W):
                opt.add(f)
            h = opt.maximize(obj) if sense == "max" else opt.minimize(obj)
            r = opt.check()
            if r == z3.unsat:
                out = self.feasibility()
                out["answer_summary"] = "no allowed value: " + out["answer_summary"]
                return out
            if r != z3.sat:
                raise ProofError("optimiser returned unknown")
            bound = str(h.value())
            if "epsilon" in bound:
                raise ProofError("the objective has no maximum or minimum because a constraint is strict (< or >); "
                                 "use <= or >= for a break-even")
            if "oo" in bound:
                beyond = obj >= real(big) if sense == "max" else obj <= real(-big)
                res, asg, _ = self.cegar_check([beyond])
                if res == "sat":
                    return {"status": "unbounded", "answer": "unbounded",
                            "answer_summary": f"{objective.src} can go beyond {fmt(big if sense == 'max' else -big)} on "
                                              f"every row",
                            "verification": [{"step": "optimise", **self.steps_mode(), "result": "unbounded",
                                              "detail": fmt(asg)}],
                            "self_check": {"matches": None}, "solver": {"mode": self.mode}, "evidence": {}}
                continue
            best = from_z3(h.value())
            asg = self.assignment(opt.model())
            size = len(self.W) if self.W is not None else 0
            nbad, new = self.violations(asg)
            self.trace.append({"solve": f"{sense}imise", "working_rows": size, "candidate": fmt(asg),
                               "violating_rows": nbad, "added": len(new)})
            if nbad:
                self.W += new
                continue
            s = p.solver()
            for _, f in self.hard(self.W):
                s.add(f)
            beyond = obj > real(best) if sense == "max" else obj < real(best)
            s.add(beyond)
            optimal = s.check() == z3.unsat
            if optimal:
                p.lean.cite(f"optimal_{sense}")
                self.lean_witness(asg, f"the optimum {fmt(best)} is reached by {assignment_text(asg)} on every "
                                       f"working row",
                                  [obj == real(best)])
                bound = self.core(beyond)
                if bound is not None:
                    p.lean.symbolic("bound", f"no allowed value has {objective.src} "
                                             f"{'above' if sense == 'max' else 'below'} {fmt(best)} "
                                             f"({len(bound)} binding row formula(s))",
                                    bound, obj <= real(best) if sense == "max" else obj >= real(best))
            s2 = p.solver()
            for _, f in self.hard(self.W):
                s2.add(f)
            s2.add(obj == real(best))
            attained = s2.check() == z3.sat
            mode = self.steps_mode()
            return {"status": "optimal" if optimal and attained else "failed", "answer": fmt(best),
                    "exact_values": {"objective": fraction_text(best)} if isinstance(best, Fraction) else {},
                    "answer_summary": f"{sense} {objective.src} = {fmt(best)}",
                    "verification": [
                        {"step": f"{sense}imise {objective.src}", **mode, "result": "sat", "detail": fmt(best)},
                        {"step": "nothing beyond the optimum is allowed",
                         "method": "Z3: constraints AND objective beyond the optimum" +
                                   (" (on the working rows; a subset only relaxes, so this holds for all rows)"
                                    if self.mode == "cegar" else ""),
                         "result": "proved (unsat)" if optimal else "failed"},
                        {"step": "the optimum is reachable",
                         "method": "Z3 witness" + (" + lake query: zero violating rows among all eligible rows"
                                                   if self.mode == "cegar" else ""),
                         "result": "proved" if attained else "failed", "detail": fmt(asg)}],
                    "self_check": {"method": "independent solver re-check of bound and attainment"
                                             + (" and lake verification over every row" if self.mode == "cegar" else ""),
                                   "matches": optimal and attained},
                    "solver": {"witness": fmt(asg), "mode": self.mode, "rounds": self.rounds,
                               "working_rows": len(self.W) if self.W is not None else 0}, "evidence": {}}
        raise ProofError(f"refinement did not converge in {max_it} rounds")

    def check_global(self, claim: Expr) -> dict:
        res, val, _ = self.cegar_check([])
        if res == "unsat":
            return {"status": "vacuous", "answer": "the constraints contradict each other, so the claim is vacuous",
                    "answer_summary": "inconsistent hypotheses",
                    "verification": [{"step": "hypotheses consistent?", **self.steps_mode(), "result": "unsat",
                                      "detail": val}], "self_check": {"matches": None}, "evidence": {}}
        tr = self.translator()
        negation = z3.Not(as_bool(tr.term(claim.body, None, self.W)))
        res, val, _ = self.cegar_check([negation])
        holds = res == "unsat"
        self.p.lean.cite("check_global")
        claim_f = as_bool(tr.term(claim.body, None, self.W))
        if holds:
            used = self.core(negation)
            if used is not None:
                self.p.lean.symbolic("claim", f"`{claim.src}` follows from {len(used)} row formula(s)", used, claim_f)
        else:
            self.lean_witness(val, f"{assignment_text(val)} satisfies the constraints and breaks `{claim.src}`",
                              [z3.Not(claim_f)])
        return {"status": "proved" if holds else "refuted", "answer": "holds" if holds else "does not hold",
                "answer_summary": f"`{claim.src}` {'follows from' if holds else 'does not follow from'} the constraints",
                "verification": [{"step": "hypotheses are consistent", **self.steps_mode(), "result": "sat"},
                                 {"step": f"negation of `{claim.src}`", "method": "Z3: hypotheses AND NOT claim",
                                  "result": "proved (unsat)" if holds else "counterexample",
                                  "detail": None if holds else fmt(val)}],
                "self_check": {"matches": None}, "solver": {"mode": self.mode}, "evidence": {}}
