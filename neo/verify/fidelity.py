"""Requirements and the fidelity check: does the model mean what the question asked?

A second language model (the requirements writer) reads the question and the ontology briefing, never the model,
and lists what the question needs as small typed claims:

  universe   which entity's rows                      {"entity": "deal"}
  filter     a per-row condition the rows must meet   {"expr": "1980 <= year <= 1989"}
  measure    what is computed                         {"aggregate": "count" | "sum" | "avg" | "share" | "min" |
                                                        "max" | "list" | "check" | "possible", "of": field}
  group      one result per value of                  {"expr": "target_state"}
  order      ranking                                  {"sense": "max" | "min", "top": 3}
  other      anything else (stated as text)            not checkable

Each requirement is checked against the validated model by the tool that suits it:

  filter     lake counts on the pinned snapshot: rows the model keeps that the requirement excludes (the model
             answers about rows the question did not ask about), and rows the question includes that the model
             drops (allowed only when every such row has a missing value the model had to leave out). Z3 then
             tries to prove model → requirement for every possible row, and the proof becomes a Lean theorem.
  universe, measure, group, order
             compared with the model's structure.

A failure goes back to the model writer with the reason, as a repair. The example rows found (key, label and
time fields) are kept for people; they reach a language model only when governance.allow_row_samples_to_llm is on.
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional, Tuple

import z3

from ..dsl import AGGREGATES
from ..dsl.parse import ExprError, aggregate_nodes, parse, refs
from ..proof.lean import Goals
from .rowlogic import RowLogic, Unsupported, implies

KINDS = ("universe", "filter", "measure", "group", "order", "other")
AGG_SYNONYMS = {"count": "count", "number": "count", "how many": "count", "sum": "sum", "total": "sum",
                "avg": "avg", "average": "avg", "mean": "avg", "share": "share", "proportion": "share",
                "percentage": "share", "fraction": "share", "min": "min", "minimum": "min", "smallest": "min",
                "lowest": "min", "max": "max", "maximum": "max", "largest": "max", "highest": "max",
                "biggest": "max", "list": "list", "which": "list", "check": "check", "is it true": "check",
                "possible": "possible", "feasible": "possible", "compare": "compare", "comparison": "compare"}


def parse_requirements(reply) -> Tuple[List[dict], List[str]]:
    """(requirements, problems) from the requirements writer's JSON reply."""
    if isinstance(reply, dict):
        items = reply.get("requirements")
    elif isinstance(reply, list):
        items = reply
    else:
        items = None
    if not isinstance(items, list):
        return [], ["the reply had no `requirements` list"]
    out, problems = [], []
    for i, r in enumerate(items, 1):
        if not isinstance(r, dict):
            problems.append(f"item {i} is not an object")
            continue
        kind = str(r.get("kind", "other")).lower()
        if kind not in KINDS:
            kind = "other"
        req = {"id": str(r.get("id") or f"R{i}"), "kind": kind, "text": str(r.get("text") or "").strip()}
        for k in ("entity", "expr", "aggregate", "of", "sense", "top"):
            if r.get(k) not in (None, ""):
                req[k] = r[k]
        if kind == "measure" and "aggregate" in req:
            req["aggregate"] = AGG_SYNONYMS.get(str(req["aggregate"]).lower(), str(req["aggregate"]).lower())
        if kind in ("filter", "group") and "expr" not in req:
            req["kind"] = "other"
        out.append(req)
    return out, problems


# ---------------------------------------------------------------------- the model's side
def model_condition(model) -> List:
    """The per-row conditions that pick the rows a model's answer is about (Expr list; empty = every row)."""
    parts = [model.rows] if model.rows is not None else []
    t = model.target or {}
    if model.form == "value" and len(t.get("columns") or {}) == 1:
        (e,) = t["columns"].values()
        if isinstance(e.body, ast.Call) and e.body.func.id == "count" and len(e.body.args) == 1:
            parts.append(parse(ast.unparse(e.body.args[0])))
    if model.form == "set" and t.get("where") is not None:
        parts.append(t["where"])
    return parts


def model_aggregates(model) -> List[Tuple[str, set]]:
    """(aggregate kind, names its argument reads) the model computes."""
    out = []
    t = model.target or {}
    exprs = list((t.get("columns") or {}).values())
    for k in ("objective", "claim"):
        if t.get(k) is not None:
            exprs.append(t[k])
    for e in exprs:
        for n in aggregate_nodes(e, AGGREGATES):
            fn = {"min_of": "min", "max_of": "max"}.get(n.func.id, n.func.id)
            names = set()
            for a in n.args:
                names |= refs(parse(ast.unparse(a)))[0]
            out.append((fn, names))
    if model.form == "extremum":
        out.append((t.get("sense", "max"), refs(t["expr"])[0] if t.get("expr") is not None else set()))
    if model.form == "set":
        out.append(("list", set()))
    if model.form == "check":
        out.append(("check", set()))
    if model.form == "feasibility":
        out.append(("possible", set()))
    if model.form == "optimisation":
        out.append((t.get("sense", "max"), set()))
    return out


def _expand_names(onto, entity: str, model, names: set) -> set:
    """Names plus the fields they read through defines and ontology expressions."""
    out, todo = set(), list(names)
    while todo:
        n = todo.pop()
        if n in out:
            continue
        out.add(n)
        src = None
        if n in (model.defines or {}):
            src = model.defines[n]
        else:
            f = onto.field(entity, n) if "." not in n else None
            if f is not None and f.expression:
                src = parse(f.expression)
        if src is not None:
            todo += list(refs(src)[0])
    return out


# ---------------------------------------------------------------------- the check
class Fidelity:
    """Checks one validated, proved model against the requirements. `prover` is the Prover that ran it: its
    compiler, parameters and query log are reused, so the counts run on the same snapshot and policies."""

    def __init__(self, onto, requirements: List[dict], cfg: dict):
        self.onto, self.reqs, self.cfg = onto, requirements, cfg
        self.goals = Goals(cfg)
        self.allow_rows = cfg.get("governance", {}).get("allow_row_samples_to_llm", False)

    def run(self, model, prover, node: str = "", plan_node: bool = False) -> Tuple[List[dict], List[str]]:
        results, errors = [], []
        entity = model.universe.entity if model.universe else None
        logic = RowLogic(self.onto, entity, model.defines) if entity else None
        cond = model_condition(model)
        m_formula = self._formula(logic, cond)
        filters = [r for r in self.reqs if r["kind"] == "filter"]
        prefix = f"{node}: " if node else ""
        for r in self.reqs:
            res = {"id": r["id"], "kind": r["kind"], "text": r["text"], "node": node or None}
            try:
                if r["kind"] == "universe":
                    self._universe(r, model, res)
                elif r["kind"] == "filter":
                    self._filter(r, model, prover, cond, logic, m_formula, res, node)
                elif r["kind"] == "measure":
                    self._measure(r, model, entity, res, plan_node)
                elif r["kind"] == "group":
                    self._group(r, model, res)
                elif r["kind"] == "order":
                    self._order(r, model, res)
                else:
                    res.update(result="not_checkable", method="stated in words only", detail=r["text"])
            except Exception as e:  # a requirement neo cannot read must not stop the run
                res.update(result="not_checkable", method="could not read the requirement", detail=str(e)[:300])
            if res["result"] in ("contradicted", "missing"):
                errors.append(f"{prefix}requirement {r['id']} ({r['text']}): {res['detail']}" + self._ex(res))
            results.append(res)
        if filters and not plan_node:
            try:
                res = self._narrower(filters, model, prover, cond, logic, m_formula, node)
            except Exception:
                res = None
            if res:
                results.append(res)
                if res["result"] == "missing":
                    errors.append(f"{prefix}{res['detail']}" + self._ex(res))
        return results, errors

    # ------------------------------------------------------------------ helpers
    def _ex(self, res) -> str:
        """Example rows for the model writer, only when governance lets row data reach a language model."""
        if not self.allow_rows or not res.get("examples"):
            return ""
        return " Example rows: " + "; ".join(str(e) for e in res["examples"][:2])

    def _formula(self, logic, parts):
        if logic is None:
            return None
        try:
            fs = [logic.formula(p) for p in parts]
        except Unsupported:
            return None
        return z3.And(*fs) if len(fs) > 1 else (fs[0] if fs else z3.BoolVal(True))

    def _sql(self, prover, parts) -> str:
        c = prover.c
        if not parts:
            return "TRUE"
        return " AND ".join(f"COALESCE({c.as_bool(c.expr(parse(p)))}, FALSE)" for p in parts)

    def _examples(self, prover, cond_sql: str, purpose: str) -> List[dict]:
        fields = prover.display_fields()
        if not fields:
            return []
        parts, decode = prover.select_fields(prover.c, fields)
        _, rows = prover.sql(purpose, f"SELECT {', '.join(parts)} FROM {prover.c.from_clause()} "
                                      f"WHERE {prover.c.where_clause(cond_sql, include_rows=False)} LIMIT 3")
        from ..proof.prover import fmt
        return [fmt(decode(row)[0]) for row in rows]

    def _count(self, prover, cond_sql: str, purpose: str) -> int:
        _, rows = prover.sql(purpose, f"SELECT COUNT(*) FROM {prover.c.from_clause()} "
                                      f"WHERE {prover.c.where_clause(cond_sql, include_rows=False)}")
        return int(rows[0][0])

    # ------------------------------------------------------------------ kinds
    def _universe(self, r, model, res):
        want = r.get("entity")
        got = model.universe.entity if model.universe else None
        if not want:
            res.update(result="not_checkable", method="no entity named", detail=r["text"])
        elif want == got:
            res.update(result="matched", method="the model's universe", detail=f"entity `{got}`")
        else:
            res.update(result="contradicted", method="the model's universe",
                       detail=f"the question is about `{want}` rows, but the model's universe is `{got}`")

    def _filter(self, r, model, prover, cond, logic, m_formula, res, node):
        expr = parse(r["expr"])
        r_sql = self._sql(prover, [expr])
        m_sql = self._sql(prover, cond)
        tag = f"{node} " if node else ""
        extra = self._count(prover, f"({m_sql}) AND NOT ({r_sql})", f"fidelity {tag}{r['id']}: rows the model keeps "
                                                                     f"that the requirement excludes")
        res.update(method="lake count on the pinned snapshot: rows the model keeps that the requirement excludes",
                   counts={"kept_but_excluded": extra}, expr=expr.src)
        if extra:
            ex = self._examples(prover, f"({m_sql}) AND NOT ({r_sql})", f"fidelity {tag}{r['id']}: example rows")
            res.update(result="contradicted", examples=ex,
                       detail=f"your model keeps {extra} row(s) that `{expr.src}` excludes. Change the filter so "
                              f"every row meets it.")
            return
        res.update(result="matched", detail=f"every row the model keeps meets `{expr.src}`")
        if logic is None or m_formula is None:
            return
        try:
            r_formula = logic.formula(expr)
        except Unsupported:
            return
        if implies(m_formula, r_formula):
            name = f"fidelity_{node}_{r['id']}" if node else f"fidelity_{r['id']}"
            self.goals.symbolic(name, f"every row the model keeps meets {r['id']} ({r['text']}), for any values of "
                                      f"the fields", [m_formula], r_formula)
            res["symbolic"] = "Z3: model → requirement for every row (unsat)"
            res["theorem"] = self.goals.items[-1]["name"] if self.goals.items else None

    def _narrower(self, filters, model, prover, cond, logic, m_formula, node) -> Optional[dict]:
        """Rows the question includes (every filter requirement) that the model leaves out."""
        exprs = [parse(r["expr"]) for r in filters]
        try:
            r_sql = self._sql(prover, exprs)
            m_sql = self._sql(prover, cond)
        except Exception:
            return None
        res = {"id": "+".join(r["id"] for r in filters), "kind": "coverage",
               "text": "no row the question includes is left out", "node": node or None,
               "method": "lake count: rows meeting every filter requirement that the model leaves out"}
        dropped = self._count(prover, f"({r_sql}) AND NOT ({m_sql})", "fidelity: rows the question includes that "
                                                                      "the model leaves out")
        res["counts"] = {"left_out": dropped}
        if not dropped:
            res.update(result="matched", detail="the model keeps every row the requirements include")
            if logic is not None and m_formula is not None:
                try:
                    r_all = z3.And(*[logic.formula(e) for e in exprs])
                except Unsupported:
                    r_all = None
                if r_all is not None and implies(r_all, m_formula):
                    self.goals.symbolic("fidelity_coverage", "every row meeting all filter requirements is kept by "
                                                             "the model, for any values of the fields", [r_all],
                                        m_formula)
                    res["symbolic"] = "Z3: requirements → model for every row (unsat)"
            return res
        req_names = set()
        for e in exprs:
            req_names |= refs(e)[0]
        entity = model.universe.entity
        model_names = set()
        for p in cond:
            model_names |= refs(p)[0]
        only_model = [n for n in _expand_names(self.onto, entity, model, model_names) - req_names
                      if prover.scope().field(n)]
        c = prover.c
        nulls = " OR ".join(c.is_null(c.expr(parse(n))) for n in sorted(only_model)) or "FALSE"
        unexplained = self._count(prover, f"({r_sql}) AND NOT ({m_sql}) AND NOT ({nulls})",
                                  "fidelity: left-out rows that have no missing value")
        res["counts"]["left_out_without_missing_value"] = unexplained
        if unexplained:
            ex = self._examples(prover, f"({r_sql}) AND NOT ({m_sql}) AND NOT ({nulls})",
                                "fidelity: example left-out rows")
            res.update(result="missing", examples=ex,
                       detail=f"the question includes {unexplained} row(s) your model leaves out although none of "
                              f"their values is missing. Include them, or narrow only for a reason the question "
                              f"gives.")
        else:
            res.update(result="narrower", detail=f"{dropped} row(s) the question includes are left out, each "
                                                 f"because a value the model needs is missing "
                                                 f"({', '.join(sorted(only_model))})")
        return res

    def _measure(self, r, model, entity, res, plan_node):
        want = r.get("aggregate")
        aggs = model_aggregates(model)
        res["method"] = "the model's target"
        if want in (None, "compare"):
            res.update(result="not_checkable", detail=r["text"])
            return
        kinds = {k for k, _ in aggs}
        ok = want in kinds or (want == "share" and "count" in kinds and model.form == "value")
        if not ok:
            got = ", ".join(sorted(kinds)) or "nothing"
            if plan_node:
                res.update(result="not_checkable", detail=f"this part computes {got}")
                return
            res.update(result="contradicted",
                       detail=f"the question asks for {want}, but the model computes {got}")
            return
        of = r.get("of")
        if of:
            names = set()
            for k, n in aggs:
                if k == want or (want == "share" and k == "count"):
                    names |= _expand_names(self.onto, entity, model, n)
            if str(of) not in names:
                res.update(result="contradicted",
                           detail=f"the question asks for the {want} of `{of}`, but the model's {want} does not "
                                  f"read `{of}`")
                return
        res.update(result="matched", detail=f"{want}" + (f" of `{of}`" if of else ""))

    def _group(self, r, model, res):
        res["method"] = "the model's group_by"
        if model.group_by is None:
            res.update(result="missing", detail=f"the question wants one result per `{r['expr']}`, but the model "
                                                f"has no group_by")
            return
        want = refs(parse(r["expr"]))[0]
        got = refs(model.group_by)[0]
        if want <= got:
            res.update(result="matched", detail=f"grouped by `{model.group_by.src}`")
        else:
            res.update(result="contradicted", detail=f"the question groups by `{r['expr']}`, the model by "
                                                     f"`{model.group_by.src}`")

    def _order(self, r, model, res):
        res["method"] = "the model's ranking"
        t = model.target or {}
        sense, top = r.get("sense"), r.get("top")
        if model.form == "extremum":
            got_sense, got_top = t.get("sense"), t.get("top", 1)
        elif model.form == "table" and t.get("sort_by"):
            got_sense, got_top = ("max" if t.get("descending") else "min"), t.get("limit")
        else:
            res.update(result="missing", detail="the question asks for a ranking, but the model does not rank")
            return
        problems = []
        if sense and sense != got_sense:
            problems.append(f"the question wants the {'largest' if sense == 'max' else 'smallest'}, the model the "
                            f"{'largest' if got_sense == 'max' else 'smallest'}")
        if top and got_top and int(top) != int(got_top):
            problems.append(f"the question wants {top}, the model returns {got_top}")
        if problems:
            res.update(result="contradicted", detail="; ".join(problems))
        else:
            res.update(result="matched", detail=f"{got_sense}" + (f", top {got_top}" if got_top else ""))


def summary(results: List[dict]) -> str:
    """matched | narrower | failed | not_checked, for the whole list."""
    if not results:
        return "not_checked"
    rs = {r["result"] for r in results}
    if rs & {"contradicted", "missing"}:
        return "failed"
    if "narrower" in rs:
        return "narrower"
    if rs <= {"not_checkable"}:
        return "not_checked"
    return "matched"
