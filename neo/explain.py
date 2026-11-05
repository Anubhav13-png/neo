"""Explain a run step by step, for the web UI: what was pinned, what the LLM was shown and wrote, the model as
mathematics, every column it touches and what that column means, the SQL, the proof, the independent check,
the certificate, the answer and the routing decision.

Everything is read from the run store and the pinned snapshot / ontology version; nothing is recomputed except
the validation of the stored model (cheap, and it resolves aliases, policies and the fields used).
"""
from __future__ import annotations

import ast
import html
import json
import os
from typing import Dict, List, Optional

from .dsl import AGGREGATES, DOCS, ExprError, parse
from .dsl.parse import refs
from .lake.profiles import profile_table, render_profile
from .semantic.validator import validate

STEPS = [
    ("pin", "Pin", False, "Freeze one version of every table, the ontology version and the rules for this run."),
    ("retrieve", "Retrieve", False, "Link the question's words to ontology terms, fields and metrics; keep only that "
                                    "part of the ontology as the LLM's briefing."),
    ("ontology", "Ontology at work", False, "Which ontology terms, fields, rules and constraints this question "
                                            "touched, why each constraint applied, and how the model handled it."),
    ("requirements", "Requirements", True, "A second LLM, which never sees the model, lists what the question "
                                           "needs: whose rows, which filters, what is computed. Each one is "
                                           "checked against the model later."),
    ("formalize", "Formalize", True, "The LLM writes the question as a JSON model in ontology vocabulary (or a plan "
                                     "of small models). The validator, prover and fidelity check test it; errors go "
                                     "back to the LLM (repair loop)."),
    ("fidelity", "Fidelity check", False, "Does the model mean what was asked? Each requirement is checked against "
                                          "the model: lake counts of rows the model keeps that the question "
                                          "excludes (and the reverse), Z3 and Lean for the filters."),
    ("math", "Constraints, term by term", False, "The model as mathematics. Every statement is read in plain words, "
                                                  "and every symbol in it is explained: where it comes from and "
                                                  "what it means."),
    ("columns", "Columns", False, "Every field the model uses: what it means, its unit and the physical column."),
    ("validate", "Validate", False, "Joins, double counting, units, time semantics, access rights, rules, cost."),
    ("execute", "Compile and run", False, "The model compiled to exact integer SQL and run on the pinned snapshot."),
    ("prove", "Solve and prove", False, "How the answer was found and proved: the exact numbers from the lake, "
                                         "what Z3 was given, each solving round, Lean's kernel re-checking Z3, and the final "
                                         "checks."),
    ("check", "Independent check", False, "A separate implementation recomputes the answer; any difference is a "
                                          "mismatch."),
    ("review", "LLM review", True, "The LLM re-reads its model and the result and may revise the model once."),
    ("certify", "Certify", False, "Code renders the proof certificate: a full one for people, an LLM-safe one "
                                  "without row data."),
    ("answer", "Answer", True, "The LLM writes the answer from the LLM-safe certificate only, with numbers as slots "
                               "that neo fills from the proved values."),
    ("answer_check", "Answer check", False, "Every number and claim in the written answer is checked by a program. "
                                            "Failures go back to the LLM; what still fails is removed, and owed "
                                            "caveats are added."),
    ("ledger", "Claim ledger", False, "One row per claim (question, model, answer): who made it, what checked it, "
                                      "the result. Lean checks the claim theorems."),
    ("route", "Route", False, "Send to human review if the answer is not a clean proof, approximate or high impact."),
]


# ---------------------------------------------------------------------- expressions as mathematics
E = html.escape
PREC = {ast.Or: 1, ast.And: 2, ast.Not: 3, ast.Compare: 4, ast.Add: 5, ast.Sub: 5, ast.Mult: 6, ast.Div: 6,
        ast.FloorDiv: 6, ast.Mod: 6}
CMP = {ast.Eq: "=", ast.NotEq: "≠", ast.Lt: "&lt;", ast.LtE: "≤", ast.Gt: "&gt;", ast.GtE: "≥", ast.In: "∈",
       ast.NotIn: "∉"}
BIN = {ast.Add: "+", ast.Sub: "−", ast.Mult: "·", ast.Div: "/", ast.Mod: "mod"}


def _num(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v.is_integer() and abs(v) >= 1000:
            v = int(v)
            for k, sup in ((12, "¹²"), (9, "⁹"), (6, "⁶"), (3, "³")):
                if v % 10 ** k == 0:
                    m = v // 10 ** k
                    return f"10{sup}" if m == 1 else f"{m}·10{sup}"
            return f"{v:,}"
        return repr(v)
    if isinstance(v, int):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    if v is None:
        return "missing"
    return f"<span class=s>'{E(str(v))}'</span>"


class MathWriter:
    """Renders model expressions as HTML mathematics. Per-row names (fields, defines) get the row variable r."""

    def __init__(self, kinds: Dict[str, str], meanings: Dict[str, str]):
        self.kinds = kinds          # name -> field | define | param | var
        self.meanings = meanings    # name -> tooltip

    def name(self, n: str, row: bool) -> str:
        kind = self.kinds.get(n, "field" if "." in n else "param")
        tip = f' title="{E(self.meanings[n])}"' if n in self.meanings else ""
        label = f"<span class={kind[0]}{tip}>{E(n)}</span>"
        return f"{label}({ROW})" if kind in ("field", "define") and row else label

    def __call__(self, src, row: bool = False, over: str = "R") -> str:
        try:
            body = parse(src).body
        except ExprError:
            return f"<code>{E(str(src))}</code>"
        return self.node(body, row, over)

    def wrap(self, node, parent: int, row, over) -> str:
        inner = self.node(node, row, over)
        p = self.prec(node)
        return f"({inner})" if p < parent else inner

    @staticmethod
    def prec(node) -> int:
        if isinstance(node, ast.BoolOp):
            return PREC[type(node.op)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return 3
        if isinstance(node, ast.Compare):
            return 4
        if isinstance(node, ast.BinOp):
            return PREC.get(type(node.op), 6)
        if isinstance(node, ast.IfExp):
            return 0
        return 9

    def node(self, n, row: bool, over: str) -> str:
        w = lambda c, p: self.wrap(c, p, row, over)
        if isinstance(n, ast.Constant):
            return _num(n.value)
        if isinstance(n, (ast.Name, ast.Attribute)):
            nm = n.id if isinstance(n, ast.Name) else f"{n.value.id}.{n.attr}"
            if nm in ("True", "False"):
                return nm.lower()
            return self.name(nm, row)
        if isinstance(n, (ast.List, ast.Tuple)):
            return "{" + ", ".join(self.node(e, row, over) for e in n.elts) + "}"
        if isinstance(n, ast.BoolOp):
            op = " ∧ " if isinstance(n.op, ast.And) else " ∨ "
            return op.join(w(v, PREC[type(n.op)] + 1) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            if isinstance(n.op, ast.Not):
                return "¬" + w(n.operand, 4)
            return ("−" if isinstance(n.op, ast.USub) else "+") + w(n.operand, 7)
        if isinstance(n, ast.BinOp):
            p = PREC.get(type(n.op), 6)
            if isinstance(n.op, ast.FloorDiv):
                return f"⌊{self.node(n.left, row, over)} / {self.node(n.right, row, over)}⌋"
            return f"{w(n.left, p)} {BIN[type(n.op)]} {w(n.right, p + 1)}"
        if isinstance(n, ast.Compare):
            parts = [w(n.left, 5)]
            for op, c in zip(n.ops, n.comparators):
                parts += [CMP[type(op)], w(c, 5)]
            return " ".join(parts)
        if isinstance(n, ast.IfExp):
            return (f"(if {self.node(n.test, row, over)} then {self.node(n.body, row, over)} "
                    f"else {self.node(n.orelse, row, over)})")
        if isinstance(n, ast.Call):
            return self.call(n.func.id, n.args, row, over)
        return f"<code>{E(ast.unparse(n))}</code>"

    def call(self, f: str, args, row: bool, over: str) -> str:
        a = lambda i, r=True: self.node(args[i], r, over)
        S = f"<i>{over}</i>"
        member = f"{ROW} ∈ {S}"
        if f in AGGREGATES:
            if f == "count":
                return f"|{S}|" if not args else f"|{{{member} : {a(0)}}}|"
            if f == "share":
                return f"|{{{member} : {a(0)}}}| / |{S}|"
            if f == "any_row":
                return f"∃ {member} : {a(0)}"
            if f == "all_rows":
                return f"∀ {member} : {a(0)}"
            sub = member + (f", {a(1)}" if len(args) > 1 else "")
            sym = {"sum": "Σ", "avg": "avg", "min_of": "min", "max_of": "max"}.get(f, f)
            return f"<span class=op>{sym}</span><sub>{sub}</sub> {self.wrap(args[0], 7, True, over)}"
        if f == "abs":
            return f"|{a(0, row)}|"
        if f == "implies":
            return f"({a(0, row)} ⇒ {a(1, row)})"
        if f == "ite":
            return f"(if {a(0, row)} then {a(1, row)} else {a(2, row)})"
        if f == "is_null":
            return f"{a(0, row)} is missing"
        if f == "not_null":
            return f"{a(0, row)} is present"
        if f == "rule_param" and len(args) == 2:
            return f"rule[{a(0, row)}].{a(1, row)}"
        return f"{E(f)}(" + ", ".join(a(i, row) for i in range(len(args))) + ")"


ROW = "<i>r</i>"


# ---------------------------------------------------------------------- the model as a list of statements
def _expr_of(v):
    return v.get("expr") if isinstance(v, dict) else v


def math_statements(raw: dict, mw: MathWriter, entity_label: str, policies: Dict[str, str],
                    joins: List[dict]) -> List[dict]:
    out: List[dict] = []
    add = lambda label, body, note="": out.append({"label": label, "html": body, "note": note})
    form = raw.get("form", "")
    over = "R"
    add("Universe", f"<i>U</i> = all rows of {entity_label}",
        "The set every row variable r ranges over, before any filter.")
    for j in joins:
        add("Join", f"each {ROW} also carries <b>{E(j['alias'])}</b> = its {E(j['entity'])} row via "
                    f"{E(j['relation'])} <span class=muted>({E(j['cardinality'])})</span>",
            "many_to_one keeps one row per universe row; one_to_many would repeat rows.")
    uni = raw.get("universe") or {}
    if uni.get("scenario_set"):
        add("Scenario set", f"<i>U</i> ← <i>U</i> ∩ {E(uni['scenario_set'])}", "An approved window of history.")
    for alias, expr in policies.items():
        add("Access policy", f"<i>U</i> ← {{ {ROW} ∈ <i>U</i> : {mw(expr, row=True)} }}",
            f"Your row policy on {alias or 'the universe'}: added by code, not by the LLM.")
    rows = raw.get("rows")
    if rows not in (None, "", "True", True):
        add("Eligible rows", f"<i>R</i> = {{ {ROW} ∈ <i>U</i> : {mw(rows, row=True)} }}",
            "The filter the LLM wrote (`rows`).")
    else:
        add("Eligible rows", "<i>R</i> = <i>U</i>", "No filter.")
    for name, e in (raw.get("define") or {}).items():
        add("Definition", f"{mw.name(name, True)} = {mw(_expr_of(e), row=True)}", "A per-row derived value.")
    if raw.get("group_by"):
        over = "R<sub>g</sub>"
        add("Groups", f"<i>g</i>({ROW}) = {mw(raw['group_by'], row=True)}; &nbsp; <i>R<sub>g</sub></i> = "
                      f"{{ {ROW} ∈ <i>R</i> : <i>g</i>({ROW}) = <i>g</i> }}", "One result row per group g.")
    for name, e in (raw.get("parameters") or {}).items():
        unit = e.get("unit") if isinstance(e, dict) else None
        add("Parameter", f"{mw.name(name, False)} = {mw(_expr_of(e), over='R')}"
                         + (f" <span class=muted>[{E(unit)}]</span>" if unit else ""),
            "A fixed number from the question, a rule, or an aggregate over R.")
    for v in raw.get("variables") or []:
        sort = {"Real": "ℝ", "Int": "ℤ", "Bool": "{true, false}"}.get(v.get("sort"), v.get("sort", ""))
        dom = f", &nbsp; {mw(v['domain'])}" if v.get("domain") else ""
        add("Unknown", f"{mw.name(v['name'], False)} ∈ {sort}{dom}"
                       + (f" <span class=muted>[{E(v['unit'])}]</span>" if v.get("unit") else ""),
            "The solver (Z3) searches for this value.")
    for c in raw.get("constraints") or []:
        add("Constraint", f"<b>{E(c.get('name', ''))}</b>: &nbsp; {mw(c.get('expr', ''))}",
            f"Must hold. Source: {c['source']}" if c.get("source") else "Must hold.")
    for c in raw.get("assumptions") or []:
        add("Assumption", f"<b>{E(c.get('name', ''))}</b>: &nbsp; {mw(c.get('expr', ''))}", "The question's own condition.")
    t = raw.get("target") or {}
    if form in ("value", "table"):
        cols = t.get("columns") or ({"answer": t["expr"]} if t.get("expr") else {})
        for name, e in cols.items():
            lhs = f"<b>{E(name)}</b>" + ("<sub><i>g</i></sub>" if raw.get("group_by") else "")
            add("Answer", f"{lhs} = {mw(e, over=over)}", "for every group g" if raw.get("group_by") else "")
        if t.get("sort_by"):
            add("Order", f"sort groups by {E(str(t['sort_by']))} {'descending' if t.get('descending') else 'ascending'}"
                         + (f", keep {t['limit']}" if t.get("limit") else ""))
    elif form == "extremum":
        sense = "largest" if t.get("sense", "max") == "max" else "smallest"
        top = t.get("top", 1)
        if raw.get("group_by"):
            add("Answer", f"the {top} group(s) <i>g</i> with the {sense} {mw(t.get('expr', ''), over=over)}")
        else:
            add("Answer", f"the {top} row(s) {ROW} ∈ <i>R</i> with the {sense} {mw(t.get('expr', ''), row=True)}",
                "Proved by a lake query: no other row beats them.")
    elif form == "set":
        where = t.get("where")
        cond = f" : {mw(where, row=True)}" if where else ""
        sel = ", ".join(mw.name(s, True) for s in t.get("select") or [])
        add("Answer", f"{{ ({sel}) &nbsp;|&nbsp; {ROW} ∈ <i>R</i>{cond} }}"
                      + (f" <span class=muted>first {t['limit']}</span>" if t.get("limit") else ""))
    elif form == "check":
        if t.get("scope") == "rows":
            add("Claim", f"∀ {ROW} ∈ <i>R</i> : {mw(t.get('claim', ''), row=True)}",
                "True if a search over every row finds no counterexample.")
        else:
            add("Claim", mw(t.get("claim", "")), "Follows from the exact aggregates, or not.")
    elif form == "feasibility":
        add("Question", "∃ values of the unknowns such that every constraint and assumption holds?",
            "Z3 returns a witness, or the clashing set of constraints.")
    elif form == "optimisation":
        sense = "maximise" if t.get("sense", "max") == "max" else "minimise"
        add("Objective", f"<b>{sense}</b> {mw(t.get('objective', ''))} &nbsp; subject to every constraint above",
            "Z3 finds the optimum and proves nothing better is allowed.")
    elif form == "not_expressible":
        add("Answer", "not expressible over this data", raw.get("interpretation", ""))
    for s in raw.get("sensitivity") or []:
        vals = ", ".join(_num(v) for v in s.get("values") or [])
        add("Sensitivity", f"re-solve for {mw.name(s.get('parameter', ''), False)} ∈ {{{vals}}}")
    return out


# ---------------------------------------------------------------------- the explanation
def _sections(raw: dict) -> Dict[str, List[str]]:
    """Expression texts per role in the model."""
    t = raw.get("target") or {}
    sec: Dict[str, List[str]] = {
        "filter (rows)": [raw.get("rows")] if raw.get("rows") else [],
        "definition": [_expr_of(e) for e in (raw.get("define") or {}).values()],
        "grouping": [raw["group_by"]] if raw.get("group_by") else [],
        "parameter": [_expr_of(e) for e in (raw.get("parameters") or {}).values()],
        "constraint": [c.get("expr") for c in raw.get("constraints") or []] +
                      [c.get("expr") for c in raw.get("assumptions") or []] +
                      [v.get("domain") for v in raw.get("variables") or [] if v.get("domain")],
        "answer": list((t.get("columns") or {}).values()) + [t.get(k) for k in ("expr", "objective", "claim", "where")
                                                             if t.get(k)] + list(t.get("select") or []),
    }
    return {k: [str(x) for x in v if x not in (None, "")] for k, v in sec.items()}


def _names(texts: List[str]) -> set:
    out = set()
    for s in texts:
        try:
            out |= refs(parse(s))[0]
        except ExprError:
            pass
    return out


def explain(neo, run_id: str) -> dict:
    run = neo.runs.run(run_id)
    if run is None:
        raise KeyError(run_id)
    principal = neo.principal(run["user"])
    snap_tables = (run["snapshot"] or {}).get("tables") or {}
    step_rows = {s["step"]: s for s in neo.runs.steps(run_id)}
    load = lambda name: neo.runs.load_step(run_id, name)
    retrieve, formalize, review = load("retrieve"), load("formalize"), load("review")
    certify, answer, route = load("certify"), load("answer"), load("route")
    requirements, ledger = load("requirements"), load("ledger")
    raw = (review or {}).get("raw") or (formalize or {}).get("raw") or neo.runs.read(run_id, "model.json")
    proof = (review or {}).get("proof") or (formalize or {}).get("proof") or neo.runs.read(run_id, "proof.json")

    attempts = []
    adir = os.path.join(neo.runs.dir(run_id), "attempts")
    if os.path.isdir(adir):
        for name in sorted(os.listdir(adir), key=lambda s: int(s.split("_")[-1].split(".")[0])):
            attempts.append(neo.runs.read(run_id, f"attempts/{name}"))

    out = {"run": {**{k: run.get(k) for k in ("id", "question", "user", "status", "state", "form", "answer_summary",
                                               "created", "seconds", "needs_review", "ontology_version", "llm")},
                   "snapshot": (run["snapshot"] or {}).get("id"), "principal": principal.as_dict()},
           "steps": [{"key": k, "label": l, "llm": llm, "about": about,
                      "ms": (step_rows.get(k) or {}).get("ms"), "done": k in step_rows} for k, l, llm, about in STEPS],
           "finished": run.get("state") == "finished"}

    # pin
    tables = []
    for t, ver in snap_tables.items():
        try:
            n = neo.catalog.row_count(t, ver)
        except Exception:
            n = None
        tables.append({"table": t, "version": ver, "rows": n})
    out["pin"] = {"snapshot": (run["snapshot"] or {}).get("id"), "tables": tables,
                  "ontology_version": run["ontology_version"],
                  "rules": [{"id": k, **v} for k, v in (run.get("rules") or {}).items()]}

    # retrieve: what the LLM is shown
    if retrieve:
        from .orchestrator import ROLE
        out["retrieve"] = {**retrieve.get("summary", {}), "clarification": retrieve.get("clarification"),
                           "briefing": retrieve.get("briefing", ""), "role": ROLE, "manual": neo.manual,
                           "functions": DOCS}

    # formalize
    out["formalize"] = {"prompt": neo.formalize_prompt(run["question"], principal), "attempts": attempts,
                        "llm_calls": neo.runs.llm_calls(run_id),
                        "failed": bool((formalize or {}).get("failed")),
                        "errors": (formalize or {}).get("errors", [])}
    out["model"] = raw
    if requirements is not None:
        out["requirements"] = requirements

    if raw and raw.get("form") == "plan":
        _explain_plan(neo, run, principal, raw, out, proof or {}, (retrieve or {}).get("summary") or {})
    elif raw:
        _explain_model(neo, run, principal, raw, out, proof or {}, (retrieve or {}).get("summary") or {})

    if proof:
        keep = ("status", "form", "answer", "answer_summary", "exact_values", "verification", "solver", "parameters",
                "stated_parameters", "eligible_rows", "universe_rows", "skipped_missing", "exactness", "backtest",
                "sensitivity", "warnings", "evidence", "seconds", "self_check", "interpretation", "explain", "lean")
        out["proof"] = {k: proof.get(k) for k in keep if k in proof}
        out["queries"] = proof.get("queries", [])
        out["fidelity"] = proof.get("fidelity") or {"status": "not_checked", "results": []}
        if proof.get("nodes"):
            out["plan"] = {"nodes": proof["nodes"], "combine": (raw.get("combine") or {}).get("columns") or {},
                           "models": raw.get("nodes") or {}}
    if review:
        out["review"] = {k: review.get(k) for k in ("verdict", "issues", "applied", "reason")}
    if certify:
        out["certify"] = {"full": certify.get("full", ""), "llm": certify.get("llm", "")}
    if answer:
        out["answer"] = answer.get("text", "")
        out["answer_check"] = {k: answer.get(k) for k in ("checked", "format", "claims", "drafts", "removed",
                                                           "added_caveats")}
    if ledger:
        out["ledger"] = {k: ledger.get(k) for k in ("rows", "lean", "summary")}
    elif run.get("state") == "finished":
        md = neo.runs.read(run_id, "answer.md", as_json=False) or ""
        out["answer"] = md.split("\n\n", 1)[-1].strip()
    if route is not None:
        rev = [r for r in neo.runs.reviews(None) if r["run_id"] == run_id]
        out["route"] = {"reasons": route.get("reasons", []), "review": rev[0] if rev else None}
    out["audit"] = _audit(neo, run_id)
    return out


def _explain_plan(neo, run, principal, raw, out, proof, summary):
    """A plan: each node explained as its own model, then merged (math and anatomy per node, columns once)."""
    from .proof.plan import node_models
    nodes, _ = node_models(raw)
    merged = {"math": [], "anatomy": [], "columns": []}
    seen = set()
    for nid, node in nodes.items():
        part: dict = {}
        _explain_model(neo, run, principal, node, part, ((proof.get("nodes") or {}).get(nid) or {}), summary)
        merged["math"].append({"label": f"Part {nid}", "html": html.escape(node.get("interpretation", "")), "note": ""})
        merged["math"] += part.get("math") or []
        merged["anatomy"] += [{**a, "label": f"{nid} · {a['label']}"} for a in part.get("anatomy") or []]
        for c in part.get("columns") or []:
            if c["name"] not in seen:
                seen.add(c["name"])
                merged["columns"].append(c)
        for k in ("validate", "ontology_use"):
            out.setdefault(k, part.get(k))
    for name, e in ((raw.get("combine") or {}).get("columns") or {}).items():
        merged["math"].append({"label": "Combine", "html": f"<b>{html.escape(name)}</b> = {html.escape(str(e))}",
                               "note": "exact fractions over the parts' proved values"})
    out.update(merged)


def _explain_model(neo, run, principal, raw, out, proof, summary):
    from .lake.catalog import Snapshot
    snapshot = Snapshot.from_dict(run["snapshot"]["tables"])
    onto = neo.ontology.load(run["ontology_version"], snapshot)
    res = validate(raw, neo._ctx(onto, run["question"], principal, snapshot))
    m = res.model
    uni = m.universe if m else None
    root = uni.entity if uni else (raw.get("universe") or {}).get("entity")
    aliases = uni.aliases if uni else {"": root}
    joins = [{"alias": j.alias, "entity": j.entity, "relation": j.relation, "cardinality": j.cardinality,
              "on": [list(p) for p in j.pairs], "from": j.from_alias or root} for j in (uni.joins if uni else [])]
    policies = {a: p.src for a, p in (m.policies.items() if m else [])}

    # every name the model uses, classified, with its meaning
    kinds: Dict[str, str] = {}
    meanings: Dict[str, str] = {}
    for n, e in (raw.get("define") or {}).items():
        kinds[n] = "define"
        meanings[n] = f"defined per row: {_expr_of(e)}"
    for n, e in (raw.get("parameters") or {}).items():
        kinds[n] = "param"
        meanings[n] = f"parameter = {_expr_of(e)}"
    for v in raw.get("variables") or []:
        kinds[v.get("name", "")] = "var"
        meanings[v.get("name", "")] = "unknown solved for by Z3"
    sections = _sections(raw)
    all_names = _names([s for v in sections.values() for s in v] + list(policies.values()))

    def resolve(name):
        if "." in name:
            alias, fname = name.split(".", 1)
            ent = aliases.get(alias)
            return (alias, onto.field(ent, fname)) if ent else (alias, None)
        return ("", onto.field(root, name)) if root else ("", None)

    used = {}
    for n in sorted(all_names):
        if n in kinds:
            continue
        alias, f = resolve(n)
        if f is not None:
            kinds[n] = "field"
            used[n] = (alias, f)
            meanings[n] = (f.description or (onto.terms[f.term].name if f.term in onto.terms else f.name)) + \
                          (f" [{f.unit}]" if f.unit else "")

    mw = MathWriter(kinds, meanings)
    ent = onto.entities.get(root)
    label = f"<b>{E(root or '?')}</b>" + (f" <span class=muted>(table {E(ent.table)}, one row per "
                                          f"{E(ent.grain or 'row')})</span>" if ent else "")
    out["math"] = math_statements(raw, mw, label, policies, joins)

    # columns: meaning, unit, physical source, profile, where it is used
    restricted = set(principal.row_policies)
    snap = snapshot.as_dict()
    engine = neo.engine.bind(snapshot)
    profiles: Dict[str, dict] = {}
    cols = []
    for n, (alias, f) in used.items():
        e = onto.entities.get(f.entity)
        roles = [r for r, texts in sections.items() if n in _names(texts)]
        if n in _names(list(policies.values())):
            roles.append("access policy")
        base = []
        if f.expression:
            for b in sorted(_names([f.expression])):
                bf = onto.field(f.entity, b)
                if bf and bf.column:
                    base.append({"name": b, "column": bf.column, "unit": bf.unit})
        prof_txt, sql_type = None, None
        col = f.column or (base[0]["column"] if len(base) == 1 else None)
        if e and e.table in snap and col:
            try:
                if e.table not in profiles:
                    profiles[e.table] = profile_table(engine, neo.catalog, e.table, snap[e.table])
                p = profiles[e.table].get(col)
                if p:
                    sql_type = p.get("type")
                    prof_txt = render_profile(p, counts=f.entity not in restricted)
            except Exception:
                pass
        term = onto.terms.get(f.term) if f.term else None
        attached = [{"id": c.id, "severity": c.severity, "description": " ".join(c.description.split())}
                    for c in onto.constraints.values() if c.target in (f.id, f.term)]
        cols.append({"name": n, "alias": alias, "entity": f.entity, "entity_name": e.name if e else f.entity,
                     "table": e.table if e else None, "column": f.column, "expression": f.expression, "base": base,
                     "sql_type": sql_type, "dtype": f.dtype, "unit": f.unit, "time_semantics": f.time_semantics,
                     "classification": f.classification, "description": f.description,
                     "term": {"id": term.id, "name": term.name, "definition": " ".join(term.definition.split())}
                     if term else None, "profile": prof_txt, "used_in": roles, "constraints": attached})
    out["columns"] = cols

    grounding = raw.get("grounding") or {}
    gcons = []
    for key in ("constraints_applied", "constraints_not_applicable"):
        for item in grounding.get(key) or []:
            c = onto.constraints.get(item.get("id")) if isinstance(item, dict) else None
            gcons.append({"id": (item or {}).get("id"), "applied": key == "constraints_applied",
                          "how": (item or {}).get("how") or (item or {}).get("why"),
                          "severity": c.severity if c else None,
                          "description": " ".join(c.description.split()) if c else None})
    out["validate"] = {"ok": not res.errors, "errors": res.errors, "warnings": res.warnings,
                       "access_denied": res.access_denied, "joins": joins,
                       "policies": [{"alias": a or root, "expr": p} for a, p in policies.items()],
                       "notes": m.notes if m else {}, "grain": uni.grain if uni else None,
                       "needs_approval": m.needs_approval if m else None, "grounding": gcons,
                       "terms": [{"id": t, "name": onto.terms[t].name} for t in grounding.get("terms") or []
                                 if t in onto.terms]}
    out["anatomy"] = anatomy(raw, mw, kinds, onto, used, policies, proof)
    out["ontology_use"] = ontology_use(neo, onto, run["question"], principal, m, raw, summary)


def _audit(neo, run_id: str) -> List[dict]:
    path = neo.audit.path
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            if run_id in line:
                e = json.loads(line)
                if (e.get("data") or {}).get("run_id") == run_id:
                    out.append(e)
    return out


def ontology_view(neo, principal) -> dict:
    """The current ontology as the user may see it (fields above their clearance are left out)."""
    from .ontology.model import CLEARANCE
    snapshot = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snapshot)
    level = CLEARANCE.index(principal.clearance)
    snap = snapshot.as_dict()
    engine = neo.engine.bind(snapshot)
    ents = []
    for e in onto.entities.values():
        prof = {}
        if e.table in snap:
            try:
                prof = profile_table(engine, neo.catalog, e.table, snap[e.table])
            except Exception:
                prof = {}
        fields, hidden = [], 0
        for f in onto.fields_of(e.id):
            if CLEARANCE.index(f.classification) > level:
                hidden += 1
                continue
            p = prof.get(f.column) if f.column else None
            term = onto.terms.get(f.term) if f.term else None
            fields.append({"name": f.name, "column": f.column, "expression": f.expression, "unit": f.unit,
                           "dtype": f.dtype, "time_semantics": f.time_semantics, "classification": f.classification,
                           "description": f.description, "term": term.name if term else None,
                           "term_definition": " ".join(term.definition.split()) if term else None,
                           "profile": render_profile(p, counts=e.id not in principal.row_policies) if p else None})
        rows = neo.catalog.row_count(e.table, snap[e.table]) if e.table in snap else None
        ents.append({"id": e.id, "name": e.name, "table": e.table, "grain": e.grain, "key": e.key,
                     "definition": e.definition, "classification": e.classification,
                     "rows": None if e.id in principal.row_policies else rows,
                     "row_policy": principal.row_policies.get(e.id), "fields": fields, "hidden": hidden})
    rels = [{"id": r.id, "from": r.from_entity, "to": r.to_entity, "on": r.on, "cardinality": r.cardinality,
             "description": r.description} for r in onto.relations.values() if r.kind == "join"]
    return {"version": onto.version, "entities": ents, "relations": rels,
            "metrics": [{"id": m.id, "name": m.name, "entity": m.entity, "expression": m.expression, "unit": m.unit,
                         "description": m.description} for m in onto.metrics.values()],
            "constraints": [{"id": c.id, "severity": c.severity, "kind": c.kind, "target": c.target,
                             "description": " ".join(c.description.split())} for c in onto.constraints.values()]}


# ---------------------------------------------------------------------- the ontology layer, explained from the live version
LINK_TYPES = ("association", "composition", "hierarchy", "derivation", "equivalence", "constrains", "overrides")


def _visible(principal, f) -> bool:
    from .ontology.model import CLEARANCE
    return CLEARANCE.index(f.classification) <= CLEARANCE.index(principal.clearance)


def ontology_overview(neo, principal) -> dict:
    """Counts, examples, structure and health checks of the current ontology version, for the UI's explainer."""
    from collections import Counter
    snapshot = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snapshot)
    snap = snapshot.as_dict()
    O = onto.objects
    fields = O["fields"]
    joins = [r for r in O["relations"] if r.kind == "join"]
    links = [r for r in O["relations"] if r.kind != "join"]
    link_types = Counter((r.description.split() or ["other"])[0] if (r.description.split() or [""])[0] in LINK_TYPES
                         else "other" for r in links)

    def example(kind):
        objs = O[kind]
        if not objs:
            return None
        o = objs[0]
        if kind == "terms":
            o = next((t for t in objs if t.aliases), o)
            return {"id": o.id, "text": f"{o.name}: {' '.join(o.definition.split())[:160]}",
                    "extra": "aliases: " + ", ".join(o.aliases[:5]) if o.aliases else ""}
        if kind == "entities":
            return {"id": o.id, "text": f"table {o.table}, one row per {o.grain or 'row'}, key {o.key}", "extra": ""}
        if kind == "fields":
            o = next((f for f in objs if f.expression and _visible(principal, f)), o)
            src = f"= {o.expression}" if o.expression else f"column {o.column}"
            return {"id": f"{o.entity}.{o.name}", "text": f"{src} [{o.unit or 'no unit'}]", "extra": o.description}
        if kind == "relations":
            o = joins[0] if joins else o
            return {"id": o.id, "text": f"{o.from_entity} → {o.to_entity}, {o.cardinality}" if o.kind == "join"
                    else o.description[:160], "extra": ""}
        if kind == "constraints":
            o = next((c for c in objs if c.severity == "block"), o)
            text = " ".join(o.description.split())
            return {"id": o.id, "text": text if len(text) <= 180 else text[:180].rsplit(" ", 1)[0] + " …",
                    "extra": f"{o.severity} · {o.kind}"}
        if kind == "metrics":
            return {"id": o.id, "text": f"{o.name} = {o.expression}", "extra": o.unit or ""}
        if kind == "scenario_sets":
            return {"id": o.id, "text": f"{o.name}: rows where {o.rows}", "extra": ""}
        return {"id": o.id, "text": f"{o.source}: {str(o.result)[:160]}", "extra": ""}

    entities = []
    for e in O["entities"]:
        fs = onto.fields_of(e.id)
        vis = [f for f in fs if _visible(principal, f)]
        restricted = e.id in principal.row_policies
        rows = neo.catalog.row_count(e.table, snap[e.table]) if e.table in snap and not restricted else None
        entities.append({"id": e.id, "name": e.name, "table": e.table, "grain": e.grain, "key": e.key,
                         "time_field": e.time_field, "definition": e.definition, "classification": e.classification,
                         "rows": rows, "row_policy": principal.row_policies.get(e.id), "fields": len(vis),
                         "hidden": len(fs) - len(vis), "formulas": sum(1 for f in vis if f.expression),
                         "with_meaning": sum(1 for f in vis if f.term or f.description),
                         "domain": e.domain if hasattr(e, "domain") else ""})
    vis_fields = [f for f in fields if _visible(principal, f)]
    units = Counter(f.unit or "none" for f in vis_fields)
    time = Counter(f.time_semantics or "none" for f in vis_fields)
    classes = Counter(f.classification for f in fields)

    # health checks
    term_ids = set(onto.terms)
    used = {f.term for f in fields if f.term} | {c.target for c in O["constraints"]}
    for r in links:
        used |= {r.source, r.target}
    rules = neo.rules.all()
    health = []
    missing_rule_terms = [(r["id"], t) for r in rules if r.get("status") == "approved"
                          for t in r.get("terms") or [] if t not in term_ids]
    if missing_rule_terms:
        health.append({"level": "bad", "title": "Approved rules cite terms that do not exist",
                       "detail": "; ".join(f"{r} → {t}" for r, t in missing_rule_terms) +
                                 ". These rules can then be found only by their trigger words."})
    orphans = sorted(t for t in term_ids if t not in used)
    if orphans:
        health.append({"level": "warn", "title": f"{len(orphans)} term(s) connect to nothing",
                       "detail": "Not on any field, constraint or link, so the model is never told which data they "
                                 "mean: " + ", ".join(orphans[:12]) + (" …" if len(orphans) > 12 else "")})
    bare = [f"{f.entity}.{f.name}" for f in vis_fields if not f.term and not f.description]
    if bare:
        health.append({"level": "warn", "title": f"{len(bare)} of {len(vis_fields)} fields have no meaning attached",
                       "detail": "No term and no description, so the model sees only the name: " +
                                 ", ".join(bare[:12]) + (" …" if len(bare) > 12 else "")})
    no_unit = [f"{f.entity}.{f.name}" for f in vis_fields if not f.unit and (f.dtype or "number") == "number"]
    if no_unit:
        health.append({"level": "warn", "title": f"{len(no_unit)} numeric field(s) have no unit",
                       "detail": "Unit checks cannot protect them: " + ", ".join(no_unit[:12])})
    if not health:
        health.append({"level": "ok", "title": "No problems found", "detail": "Every term is attached, every field has a meaning."})

    versions = neo.store.list_versions()
    current = neo.store.current_version()
    return {
        "version": onto.version, "current": current, "snapshot": snapshot.id,
        "counts": {k: len(v) for k, v in O.items()},
        "detail": {"joins": len(joins), "links": len(links),
                   "block": sum(1 for c in O["constraints"] if c.severity == "block"),
                   "info": sum(1 for c in O["constraints"] if c.severity != "block"),
                   "formulas": sum(1 for f in fields if f.expression), "columns": sum(1 for f in fields if f.column)},
        "examples": {k: example(k) for k in O},
        "term_kinds": Counter(t.kind for t in O["terms"]).most_common(),
        "entities": entities,
        "joins": [{"id": r.id, "from": r.from_entity, "to": r.to_entity, "alias": r.alias, "cardinality": r.cardinality,
                   "on": " and ".join(f"{a} = {b}" for a, b in r.on), "description": r.description} for r in joins],
        "units": units.most_common(), "time": time.most_common(), "classification": classes.most_common(),
        "hidden_fields": len(fields) - len(vis_fields), "clearance": principal.clearance,
        "row_policies": principal.row_policies,
        "constraints": [{"id": c.id, "severity": c.severity, "kind": c.kind, "target": c.target,
                         "triggers": c.triggers[:8], "description": " ".join(c.description.split()),
                         "formula": c.formula} for c in O["constraints"]],
        "constraint_kinds": Counter(c.kind for c in O["constraints"]).most_common(),
        "metrics": [{"id": m.id, "name": m.name, "entity": m.entity, "expression": m.expression, "unit": m.unit}
                    for m in O["metrics"]],
        "scenario_sets": [{"id": s.id, "name": s.name, "entity": s.entity, "rows": s.rows,
                           "approved_by": s.approved_by} for s in O["scenario_sets"]],
        "link_types": link_types.most_common(),
        "rules": [{"id": r["id"], "name": r.get("name"), "status": r.get("status"), "version": r.get("version"),
                   "citation": r.get("citation"), "formula": r.get("formula"),
                   "parameters": {k: v.get("value") for k, v in (r.get("parameters") or {}).items()}} for r in rules],
        "versions": [{**v, "is_current": v["version"] == current} for v in versions],
        "health": health,
    }


def ontology_trace(neo, principal, text: str) -> dict:
    """What the linker does with some words: matched ontology objects, and for each the fields and columns behind it."""
    snapshot = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snapshot)
    out = []
    for l in neo.ontology.linker(onto).link(text, limit=12):
        d = _link_detail(onto, principal, l.id, l.kind, l.matched, l.score)
        if d:
            out.append(d)
    return {"text": text, "links": out, "unlinked": neo.ontology.linker(onto).unlinked_words(text)}


# ---------------------------------------------------------------------- statements in plain words, term by term
OPS = {
    "all_rows": ("∀", "for every eligible row"), "any_row": ("∃", "for at least one eligible row"),
    "sum": ("Σ", "the total over the rows"), "count": ("|…|", "how many rows"), "share": ("|…| / |R|", "the share of rows"),
    "avg": ("avg", "the average over the rows"), "min_of": ("min", "the smallest value over the rows"),
    "max_of": ("max", "the largest value over the rows"), "abs": ("|x|", "absolute value"),
    "implies": ("⇒", "if the left holds, the right must hold"), "is_null": ("missing", "the value is missing"),
    "not_null": ("present", "the value is present"), "coalesce": ("coalesce", "the value, or a default when missing"),
    "rule_param": ("rule[…]", "a number from an approved rule"), "year_of": ("year", "the calendar year of a date"),
    "contains": ("contains", "text contains (ignoring case)"), "min": ("min", "the smaller of two values"),
    "max": ("max", "the larger of two values"), "ite": ("if", "choose a value by a condition"),
    ast.And: ("∧", "and: both must hold"), ast.Or: ("∨", "or: at least one must hold"), ast.Not: ("¬", "not"),
    ast.Eq: ("=", "equals"), ast.NotEq: ("≠", "is not equal to"), ast.Lt: ("<", "is less than"),
    ast.LtE: ("≤", "is at most"), ast.Gt: (">", "is more than"), ast.GtE: ("≥", "is at least"),
    ast.In: ("∈", "is one of"), ast.NotIn: ("∉", "is not one of"), ast.Add: ("+", "plus"), ast.Sub: ("−", "minus"),
    ast.Mult: ("·", "times"), ast.Div: ("/", "divided by (exact fraction, never rounded)"),
    ast.FloorDiv: ("⌊ / ⌋", "divided and rounded down"), ast.Mod: ("mod", "remainder after division"),
    ast.USub: ("−", "negative of"), ast.IfExp: ("if", "choose a value by a condition"),
}
ENG_CMP = {ast.Eq: "equals", ast.NotEq: "is not", ast.Lt: "is less than", ast.LtE: "is at most",
           ast.Gt: "is more than", ast.GtE: "is at least", ast.In: "is one of", ast.NotIn: "is not one of"}
ENG_BIN = {ast.Add: "plus", ast.Sub: "minus", ast.Mult: "times", ast.Div: "divided by", ast.Mod: "modulo"}


class EnglishWriter:
    """Reads a model expression aloud."""

    def __init__(self, kinds: Dict[str, str], nouns: Dict[str, str]):
        self.kinds, self.nouns = kinds, nouns

    def __call__(self, src, over: str = "the eligible rows") -> str:
        try:
            return self.node(parse(src).body, over)
        except ExprError:
            return str(src)

    def name(self, n: str) -> str:
        kind = self.kinds.get(n, "field")
        noun = self.nouns.get(n, n)
        if kind in ("field", "define"):
            return f"the row's {noun}"
        if kind == "var":
            return f"the unknown {n}"
        return noun

    def node(self, n, over) -> str:
        w = lambda x: self.node(x, over)
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, str):
                return f"'{v}'"
            if isinstance(v, bool) or v is None:
                return {True: "true", False: "false", None: "missing"}[v]
            return f"{v:,}" if isinstance(v, int) and abs(v) >= 10000 else (
                f"{int(v):,}" if isinstance(v, float) and v.is_integer() else repr(v))
        if isinstance(n, (ast.Name, ast.Attribute)):
            nm = n.id if isinstance(n, ast.Name) else f"{n.value.id}.{n.attr}"
            return nm.lower() if nm in ("True", "False") else self.name(nm)
        if isinstance(n, (ast.List, ast.Tuple)):
            return "[" + ", ".join(w(e) for e in n.elts) + "]"
        if isinstance(n, ast.BoolOp):
            return (" and " if isinstance(n.op, ast.And) else " or ").join(w(v) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            return f"not ({w(n.operand)})" if isinstance(n.op, ast.Not) else f"minus {w(n.operand)}"
        if isinstance(n, ast.BinOp):
            if isinstance(n.op, ast.FloorDiv):
                return f"{w(n.left)} divided by {w(n.right)}, rounded down"
            return f"{w(n.left)} {ENG_BIN[type(n.op)]} {w(n.right)}"
        if isinstance(n, ast.Compare):
            parts = [w(n.left)]
            for op, c in zip(n.ops, n.comparators):
                parts += [ENG_CMP[type(op)], w(c)]
            return " ".join(parts)
        if isinstance(n, ast.IfExp):
            return f"{w(n.body)} if {w(n.test)}, otherwise {w(n.orelse)}"
        if isinstance(n, ast.Call):
            f, a = n.func.id, n.args
            if f == "count":
                return f"the number of {over}" if not a else f"the number of {over} where {w(a[0])}"
            if f == "share":
                return f"the share of {over} where {w(a[0])}"
            if f in ("sum", "avg", "min_of", "max_of"):
                word = {"sum": "the total of", "avg": "the average of", "min_of": "the smallest", "max_of": "the largest"}[f]
                cond = f" where {w(a[1])}" if len(a) > 1 else ""
                return f"{word} {w(a[0])} over {over}{cond}"
            if f == "all_rows":
                return f"for every one of {over}: {w(a[0])}"
            if f == "any_row":
                return f"for at least one of {over}: {w(a[0])}"
            if f == "abs":
                return f"the absolute value of {w(a[0])}"
            if f in ("min", "max"):
                return f"the {'smaller' if f == 'min' else 'larger'} of {w(a[0])} and {w(a[1])}"
            if f == "implies":
                return f"if {w(a[0])}, then {w(a[1])}"
            if f == "ite":
                return f"{w(a[1])} if {w(a[0])}, otherwise {w(a[2])}"
            if f == "is_null":
                return f"{w(a[0])} is missing"
            if f == "not_null":
                return f"{w(a[0])} is present"
            if f == "coalesce":
                return f"{w(a[0])}, or {w(a[1])} when it is missing"
            if f == "year_of":
                return f"the year of {w(a[0])}"
            if f == "contains":
                return f"{w(a[0])} contains {w(a[1])}"
            if f == "rule_param":
                return f"the approved value of rule {w(a[0])}, parameter {w(a[1])}"
            return f"{f}(" + ", ".join(w(x) for x in a) + ")"
        return ast.unparse(n)


def _ops_in(src) -> List[dict]:
    try:
        tree = parse(src).body
    except ExprError:
        return []
    seen, out = set(), []
    for n in ast.walk(tree):
        keys = []
        if isinstance(n, ast.Call):
            keys = [n.func.id]
        elif isinstance(n, (ast.BoolOp, ast.BinOp, ast.UnaryOp)):
            keys = [type(n.op)]
        elif isinstance(n, ast.Compare):
            keys = [type(o) for o in n.ops]
        elif isinstance(n, ast.IfExp):
            keys = [ast.IfExp]
        for k in keys:
            if k in OPS and k not in seen and not (k is ast.UAdd):
                seen.add(k)
                sym, meaning = OPS[k]
                out.append({"symbol": sym, "meaning": meaning})
    return out


def anatomy(raw: dict, mw, kinds: Dict[str, str], onto, used: dict, policies: Dict[str, str], proof: dict) -> List[dict]:
    """Each statement of the model: where it comes from, its mathematics, a plain reading, and every symbol in it."""
    params = (proof or {}).get("parameters") or {}
    witness = ((proof or {}).get("solver") or {}).get("witness") or {}
    pdefs = {n: e for n, e in (raw.get("parameters") or {}).items()}
    defines = {n: _expr_of(e) for n, e in (raw.get("define") or {}).items()}
    variables = {v.get("name"): v for v in raw.get("variables") or []}
    nouns = {}
    for n, (alias, f) in used.items():
        term = onto.terms.get(f.term) if f.term else None
        noun = (term.name if term else (f.description or f.name).split(";")[0].split(" (")[0][:60]).lower()
        nouns[n] = noun if f"({n.lower()})" in noun or noun == n.lower() else f"{noun} ({n})"
    for n in defines:
        nouns[n] = n
    ew = EnglishWriter(kinds, nouns)

    def gloss(src) -> List[dict]:
        out = []
        for n in sorted(_names([src])):
            k = kinds.get(n)
            if n in used:
                alias, f = used[n]
                term = onto.terms.get(f.term) if f.term else None
                ent = onto.entities.get(f.entity)
                out.append({"symbol": n, "kind": "field",
                            "what": f"a column of {f.entity}" + (f" (joined as {alias})" if alias else ""),
                            "meaning": f.description or (" ".join(term.definition.split()) if term else ""),
                            "term": term.name if term else None, "unit": f.unit, "time": f.time_semantics,
                            "source": f"{ent.table}.{f.column}" if f.column and ent else f"= {f.expression}",
                            "value": None})
            elif k == "define":
                out.append({"symbol": n, "kind": "definition", "what": "a value the model defines for each row",
                            "meaning": ew(defines[n]), "source": defines[n], "unit": None, "value": None})
            elif k == "param":
                e = pdefs.get(n)
                unit = e.get("unit") if isinstance(e, dict) else None
                stated = n in ((proof or {}).get("stated_parameters") or [])
                out.append({"symbol": n, "kind": "parameter",
                            "what": "a number stated in the question" if stated else "a fixed number for this model",
                            "meaning": ew(_expr_of(e)) if e is not None else "", "source": str(_expr_of(e)),
                            "unit": unit, "value": params.get(n)})
            elif k == "var":
                v = variables.get(n, {})
                out.append({"symbol": n, "kind": "unknown", "what": f"a {v.get('sort', 'Real')} number Z3 searches for",
                            "meaning": f"allowed range: {v['domain']}" if v.get("domain") else "no range given",
                            "source": "variables", "unit": v.get("unit"), "value": witness.get(n)})
        return out

    items = []

    def add(label, origin, src, row=False, over="R"):
        if src in (None, "", True, "True"):
            return
        src = str(src)
        items.append({"label": label, "origin": origin, "src": src, "math": mw(src, row=row, over=over),
                      "english": ew(src, "the rows of the group" if over != "R" else "the eligible rows"),
                      "symbols": gloss(src), "operators": _ops_in(src)})

    for alias, expr in policies.items():
        add("Access policy", "added by code from your role (the LLM did not write it)", expr, row=True)
    add("Eligible rows", "written by the LLM (`rows`)", raw.get("rows"), row=True)
    for n, e in defines.items():
        add(f"Definition {n}", "written by the LLM (`define`)", e, row=True)
    if raw.get("group_by"):
        add("Groups", "written by the LLM (`group_by`)", raw["group_by"], row=True)
    for n, e in pdefs.items():
        add(f"Parameter {n}", "written by the LLM (`parameters`)", _expr_of(e))
    for n, v in variables.items():
        if v.get("domain"):
            add(f"Range of {n}", "written by the LLM (`variables`)", v["domain"])
    for c in raw.get("constraints") or []:
        src = c.get("source") or ""
        origin = (f"written by the LLM, citing approved rule {src[5:]}" if src.startswith("rule:") else
                  f"written by the LLM, from ontology constraint {src[9:]}" if src.startswith("ontology:") else
                  "written by the LLM (`constraints`)")
        add(f"Constraint {c.get('name', '')}", origin, c.get("expr"))
    for c in raw.get("assumptions") or []:
        add(f"Assumption {c.get('name', '')}", "the question's own condition (`assumptions`)", c.get("expr"))
    t = raw.get("target") or {}
    over = "R<sub>g</sub>" if raw.get("group_by") else "R"
    for n, e in (t.get("columns") or ({"answer": t["expr"]} if t.get("expr") and raw.get("form") == "value" else {})).items():
        add(f"Answer {n}", "what to compute (`target`)", e, over=over)
    if raw.get("form") == "extremum" and t.get("expr"):
        add("Ranked value", f"what to rank, {'largest' if t.get('sense', 'max') == 'max' else 'smallest'} first",
            t["expr"], row=not raw.get("group_by"), over=over)
    if t.get("where"):
        add("Members", "which rows to list (`target.where`)", t["where"], row=True)
    if t.get("claim"):
        add("Claim", "what to prove (`target.claim`)", t["claim"], row=t.get("scope") == "rows")
    if t.get("objective"):
        add("Objective", f"what to {'maximise' if t.get('sense', 'max') == 'max' else 'minimise'}", t["objective"])
    return items


def _link_detail(onto, principal, lid: str, kind: str, matched: str = "", score=None) -> Optional[dict]:
    o = onto.by_id.get(lid)
    if o is None:
        return None
    fs = [o] if kind == "field" else [f for f in onto.objects["fields"] if f.term == lid] if kind == "term" else []
    shown = [f for f in fs if _visible(principal, f)]
    meaning = getattr(o, "definition", "") or getattr(o, "description", "") or ""
    if kind == "field":
        t = onto.terms.get(o.term) if o.term else None
        meaning = o.description or (f"{t.name}: {t.definition}" if t else "") or \
            (f"= {o.expression}" if o.expression else "no description in the ontology")
    if kind == "entity":
        meaning = f"table {o.table}, one row per {o.grain}. {o.definition}"
    if kind == "metric":
        meaning = f"{o.expression} [{o.unit or ''}] {o.description}"
    return {"id": lid, "kind": kind, "score": round(score, 3) if score is not None else None, "matched": matched,
            "name": getattr(o, "name", "") or lid, "meaning": " ".join(str(meaning).split())[:300],
            "fields": [{"entity": f.entity, "name": f.name, "column": f.column, "expression": f.expression,
                        "table": onto.entities[f.entity].table, "unit": f.unit, "time": f.time_semantics,
                        "classification": f.classification} for f in shown[:6]],
            "hidden": len(fs) - len(shown)}


def ontology_use(neo, onto, question: str, principal, m, raw: dict, summary: dict) -> dict:
    """What the ontology contributed to this run, and why each constraint applied."""
    import re
    from .ontology.service import WORD_ONLY_KINDS, triggered_block_constraints
    links = [d for d in (_link_detail(onto, principal, l["id"], l["kind"], l.get("matched", ""), l.get("score"))
                         for l in summary.get("links") or []) if d]
    used_fields = [f for _, f in (m.used_fields if m else [])]
    used_entities = set(m.universe.aliases.values()) if m and m.universe else set()
    used_targets = set(used_entities)
    for f in used_fields:
        used_targets |= onto.targets_of_field(f)
    fired = {c.id for c in triggered_block_constraints(onto, question, used_fields, used_entities)} if m else set()
    g = raw.get("grounding") or {}
    handled = {}
    for key, need in (("constraints_applied", "how"), ("constraints_not_applicable", "why")):
        for item in g.get(key) or []:
            if isinstance(item, dict) and item.get("id"):
                handled[item["id"]] = {"applied": key == "constraints_applied", "text": item.get(need, "")}
    q = question.lower()
    ids = list(dict.fromkeys(list(summary.get("constraints") or []) + sorted(fired) + list(handled)))
    cons = []
    for cid in ids:
        c = onto.constraints.get(cid)
        if c is None:
            continue
        words = [k for k in c.triggers if k and re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", q)]
        by_use = c.kind not in WORD_ONLY_KINDS and c.target in used_targets
        why = []
        if words:
            why.append("the question says " + ", ".join(f"“{w}”" for w in words))
        if by_use:
            why.append(f"the model reads {c.target}")
        cons.append({"id": c.id, "severity": c.severity, "kind": c.kind, "target": c.target,
                     "description": " ".join(c.description.split()), "formula": c.formula,
                     "enforced": c.id in fired, "why": why, "handled": handled.get(c.id),
                     "word_only": c.kind in WORD_ONLY_KINDS})
    cited = [str(c.get("source", ""))[5:] for c in raw.get("constraints") or [] if str(c.get("source", "")).startswith("rule:")]
    rules = []
    for rid in dict.fromkeys(list(summary.get("rules") or []) + cited):
        try:
            r = neo.rules.get(rid)
        except Exception:
            continue
        rules.append({"id": rid, "name": r.get("name"), "status": r.get("status"), "formula": r.get("formula"),
                      "cited": rid in cited,
                      "parameters": {k: v.get("value") for k, v in (r.get("parameters") or {}).items()}})
    return {"links": links, "entities": summary.get("entities") or [], "neighbours": summary.get("neighbours") or [],
            "hidden_fields": summary.get("hidden_fields", 0), "unlinked": summary.get("unlinked_words") or [],
            "terms_cited": [{"id": t, "name": onto.terms[t].name} for t in g.get("terms") or [] if t in onto.terms],
            "constraints": cons, "rules": rules,
            "metrics": summary.get("metrics") or [], "scenario_sets": summary.get("scenario_sets") or []}
