"""Plans: a question with several parts, as small models proved one at a time and then combined exactly.

  {"form": "plan", "interpretation": "...",
   "nodes": {"N1": <value model with a single `expr` target>, "N2": ...},
   "combine": {"columns": {"failed_bigger": "N1 > N2", "ratio": "N1 / N2"}},
   "grounding": {...}}                    # applies to every node, merged with the node's own grounding

Each node is validated and proved on its own (the value form: exact lake aggregates, Z3, the independent
recomputation). A mistake in one part is reported for that part, and the repair message names the node. The
combine expressions use only the node ids, numbers, arithmetic, comparisons and and/or/not; they are evaluated with
exact fractions, Z3 proves each combined value unique, and Lean checks `combined = f(node values)` with the node
values it already checked (Neo.Compose).
"""
from __future__ import annotations

import ast
import re
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import z3

from ..dsl.parse import ExprError, parse
from ..util import fmt_number, fraction_text, to_fraction
from .lean import Goals, check as lean_check, summary as lean_summary
from .z3expr import from_z3, real

NODE_ID = re.compile(r"^N[0-9]+$")
COMBINE_OPS = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.BinOp,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
               ast.Name, ast.Load, ast.Constant, ast.Call)


def node_models(raw: dict) -> Tuple[Dict[str, dict], List[str]]:
    """The nodes as standalone value models (plan grounding merged in), and shape errors."""
    errors: List[str] = []
    nodes = raw.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return {}, ["a plan needs `nodes`: {\"N1\": <value model>, \"N2\": ...}"]
    if len(nodes) > 8:
        errors.append("a plan may have at most 8 nodes")
    g = raw.get("grounding") or {}
    out = {}
    for nid, node in nodes.items():
        if not NODE_ID.match(str(nid)):
            errors.append(f"node id `{nid}`: use N1, N2, ...")
            continue
        if not isinstance(node, dict):
            errors.append(f"node {nid} must be a model object")
            continue
        if node.get("form", "value") != "value" or not (node.get("target") or {}).get("expr"):
            errors.append(f"node {nid}: each node is a `value` model with one `target.expr` (a single number)")
            continue
        ng = node.get("grounding") or {}
        merged = {}
        for key in ("terms", "relations", "constraints_applied", "constraints_not_applicable"):
            items = list(g.get(key) or []) + [x for x in ng.get(key) or [] if x not in (g.get(key) or [])]
            if items:
                merged[key] = items
        interp = node.get("interpretation") or f"{raw.get('interpretation', '')} (part {nid})"
        out[str(nid)] = {**node, "form": "value", "interpretation": interp, "grounding": merged}
    comb = raw.get("combine") or {}
    cols = comb.get("columns") if isinstance(comb, dict) else None
    if cols is None and isinstance(comb, dict) and comb.get("expr"):
        cols = {"value": comb["expr"]}
    if not isinstance(cols, dict) or not cols:
        errors.append("a plan needs `combine`: {\"columns\": {name: expression over N1, N2, ...}}")
    else:
        for name, text in cols.items():
            try:
                combine_expr(text, set(out))
            except ExprError as e:
                errors.append(f"combine.{name}: {e}")
    return out, errors


def combine_columns(raw: dict) -> Dict[str, str]:
    comb = raw.get("combine") or {}
    return dict(comb.get("columns") or ({"value": comb["expr"]} if comb.get("expr") else {}))


def combine_expr(text, ids: set):
    e = parse(text)
    funcs = {id(n.func) for n in ast.walk(e.body) if isinstance(n, ast.Call)}
    for n in ast.walk(e.body):
        if not isinstance(n, COMBINE_OPS):
            raise ExprError(f"`{type(n).__name__}` is not allowed in combine (in `{e.src}`)")
        if isinstance(n, ast.Call) and n.func.id not in ("abs", "min", "max"):
            raise ExprError(f"only abs, min and max may be called in combine (in `{e.src}`)")
        if isinstance(n, ast.Name) and id(n) not in funcs and n.id not in ids and n.id not in ("True", "False"):
            raise ExprError(f"`{n.id}` is not a node of this plan (nodes: {', '.join(sorted(ids))})")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (int, float, bool)):
            raise ExprError(f"only numbers may appear in combine (in `{e.src}`)")
    return e


class _Eval:
    """Exact evaluation of a combine expression: Python value (Fraction / bool / None) and the Z3 term."""

    def __init__(self, values: Dict[str, object]):
        self.values = values

    def __call__(self, n):
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool):
                return v, z3.BoolVal(v)
            f = to_fraction(v)
            return f, z3.RealVal(f)
        if isinstance(n, ast.Name):
            if n.id in ("True", "False"):
                return n.id == "True", z3.BoolVal(n.id == "True")
            v = self.values[n.id]
            if v is None:
                return None, None
            if isinstance(v, bool):
                return v, z3.BoolVal(v)
            return v, z3.RealVal(v)
        if isinstance(n, ast.BoolOp):
            parts = [self(x) for x in n.values]
            if any(p[0] is None for p in parts):
                return None, None
            vals = [bool(p[0]) for p in parts]
            if isinstance(n.op, ast.And):
                return all(vals), z3.And(*[p[1] for p in parts])
            return any(vals), z3.Or(*[p[1] for p in parts])
        if isinstance(n, ast.UnaryOp):
            v, t = self(n.operand)
            if v is None:
                return None, None
            if isinstance(n.op, ast.Not):
                return (not v), z3.Not(t)
            return (-v if isinstance(n.op, ast.USub) else v), (-t if isinstance(n.op, ast.USub) else t)
        if isinstance(n, ast.BinOp):
            (a, ta), (b, tb) = self(n.left), self(n.right)
            if a is None or b is None:
                return None, None
            if isinstance(n.op, ast.Add):
                return a + b, ta + tb
            if isinstance(n.op, ast.Sub):
                return a - b, ta - tb
            if isinstance(n.op, ast.Mult):
                return a * b, ta * tb
            if b == 0:
                return None, None
            return Fraction(a) / Fraction(b), ta / tb
        if isinstance(n, ast.Compare):
            out_v, out_t, left = True, [], self(n.left)
            for op, right_n in zip(n.ops, n.comparators):
                right = self(right_n)
                if left[0] is None or right[0] is None:
                    return None, None
                a, b, ta, tb = left[0], right[0], left[1], right[1]
                v, t = {ast.Eq: (a == b, ta == tb), ast.NotEq: (a != b, ta != tb), ast.Lt: (a < b, ta < tb),
                        ast.LtE: (a <= b, ta <= tb), ast.Gt: (a > b, ta > tb), ast.GtE: (a >= b, ta >= tb)}[type(op)]
                out_v, left = out_v and v, right
                out_t.append(t)
            return out_v, (z3.And(*out_t) if len(out_t) > 1 else out_t[0])
        if isinstance(n, ast.Call):
            args = [self(a) for a in n.args]
            if any(a[0] is None for a in args):
                return None, None
            if n.func.id == "abs":
                v, t = args[0]
                return abs(v), z3.If(t >= 0, t, -t)
            (a, ta), (b, tb) = args
            if n.func.id == "min":
                return min(a, b), z3.If(ta <= tb, ta, tb)
            return max(a, b), z3.If(ta >= tb, ta, tb)
        raise ExprError(f"cannot evaluate `{ast.unparse(n)}`")


def prove_plan(raw: dict, provers: Dict[str, object], cfg: dict) -> dict:
    """Prove every node (Provers built for the validated node models), then combine the node values."""
    from .prover import fmt
    node_proofs: Dict[str, dict] = {}
    values: Dict[str, object] = {}
    goals = Goals(cfg)
    goals.cite("value")
    goals.cite("combine")
    lean_values, queries, skipped = [], [], {}
    for nid, p in provers.items():
        proof = p.run()
        node_proofs[nid] = proof
        exact = (proof.get("exact_values") or {}).get("value")
        v = proof.get("answer")
        values[nid] = Fraction(exact) if exact else (to_fraction(v) if isinstance(v, (int, float)) and
                                                     not isinstance(v, bool) else v)
        lean_values += p.lean_values
        queries += [{**q, "purpose": f"{nid}: {q['purpose']}"} for q in proof.get("queries") or []]
        for k, n in (proof.get("skipped_missing") or {}).items():
            skipped[f"{nid}: {k}"] = n
    combined, exact_text, z3_log, unique_all = {}, {}, [], True
    combine_facts = []
    for name, text in combine_columns(raw).items():
        e = combine_expr(text, set(values))
        v, term = _Eval(values)(e.body)
        combined[name] = v
        if v is None:
            continue
        if isinstance(v, Fraction) and v.denominator != 1:
            exact_text[name] = fraction_text(v)
        is_bool = isinstance(v, bool)
        ans = z3.Bool("ans") if is_bool else z3.Real("ans")
        s = z3.Solver()
        s.add(ans == term)
        s.add(ans != (z3.BoolVal(v) if is_bool else real(v)))
        unique = s.check() == z3.unsat
        unique_all &= unique
        z3_log.append({"what": f"{name} = {e.src}", "value": fmt_number(v) if not is_bool else v,
                       "result": "unsat: no other value is possible" if unique else "sat: not unique",
                       "smt": s.sexpr()[:2000]})
        combine_facts.append(term == (z3.BoolVal(v) if is_bool else real(v)))
    for nid, v in values.items():
        if isinstance(v, Fraction) and v.denominator != 1:
            exact_text[nid] = fraction_text(v)
    nodes_ok = all(np.get("status") == "proved" for np in node_proofs.values())
    matches = [np.get("self_check", {}).get("matches") for np in node_proofs.values()]
    status = "mismatch" if any(m is False for m in matches) else \
        ("proved" if nodes_ok and unique_all and all(v is not None for v in combined.values()) else
         ("undefined" if any(v is None for v in combined.values()) else "failed"))
    answer = {**{nid: fmt(v) for nid, v in values.items()}, **{k: fmt(v) for k, v in combined.items()}}
    verification = []
    for nid, np in node_proofs.items():
        verification.append({"step": f"{nid}: {np.get('interpretation', '')}",
                             "method": f"value form over {np.get('eligible_rows', 0):,} eligible rows: exact lake "
                                       f"aggregates, Z3, independent recomputation",
                             "result": np.get("status"), "detail": np.get("answer")})
    verification.append({"step": f"combine {len(combined)} value(s) from {len(values)} proved part(s)",
                         "method": "exact fractions; Z3 proves each combined value unique",
                         "result": "proved" if unique_all else "failed",
                         "detail": {k: fmt(v) for k, v in combined.items()}})
    proof = {"status": status, "form": "plan", "interpretation": raw.get("interpretation", ""),
             "answer": answer, "exact_values": exact_text,
             "answer_summary": "; ".join(f"{k} = {answer[k]}" for k in answer),
             "nodes": {nid: _compact_node(np) for nid, np in node_proofs.items()},
             "verification": verification,
             "self_check": {"method": "each part recomputed independently",
                            "matches": None if any(m is None for m in matches) else all(matches),
                            "exact": all(np.get("self_check", {}).get("exact", True) for np in node_proofs.values())},
             "eligible_rows": sum(np.get("eligible_rows") or 0 for np in node_proofs.values()),
             "universe_rows": max((np.get("universe_rows") or 0 for np in node_proofs.values()), default=0),
             "parameters": {}, "stated_parameters": [], "skipped_missing": skipped, "queries": queries,
             "exactness": {"exact": all((np.get("exactness") or {}).get("exact", True) for np in node_proofs.values()),
                           "inexact": sorted({x for np in node_proofs.values()
                                              for x in (np.get("exactness") or {}).get("inexact", [])})},
             "evidence": {}, "explain": {"z3": z3_log + [z for np in node_proofs.values()
                                                         for z in (np.get("explain") or {}).get("z3", [])][:20]},
             "seconds": round(sum(np.get("seconds") or 0 for np in node_proofs.values()), 2)}
    if lean_values:
        goals.compute("values", "each part's value is what the lake's exact aggregates give", lean_values)
    if combine_facts:
        goals.compute("combine", "each combined value follows from the parts' proved values", combine_facts)
    proof["lean"] = lean_check(goals, cfg, f"neo plan check: {raw.get('interpretation', '')}")
    if proof["lean"]["status"] != "not_checked":
        proof["verification"].append(lean_summary(proof["lean"]))
    return proof


def _compact_node(np: dict) -> dict:
    keep = ("status", "interpretation", "answer", "answer_summary", "exact_values", "eligible_rows", "universe_rows",
            "self_check", "verification", "skipped_missing", "exactness")
    return {k: np.get(k) for k in keep if k in np}
