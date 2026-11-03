"""Lean: a second, independent checker for what Z3 decided.

Z3 is a solver: it searches, optimises and finds clashes, and its "unsat" is trusted. Lean is a proof checker
with a small kernel. For each run the prover collects goals here; this module writes them as one Lean file of
theorems about the exact formulas Z3 was given, and Lean's kernel checks them.

  compute   facts about concrete numbers (a value derived from the lake's aggregates, a witness satisfying
            every working row, the optimum reached): proved by `decide +kernel`, which is evaluation by the
            kernel itself. A rejection means Lean disagrees with Z3: the run goes to human review.
  symbolic  statements for every value of the unknowns (no allowed value beats the optimum, the constraints
            clash, the claim follows): proved by `grind` from the few rows in Z3's unsat core. Lean's
            automation may fail on non-linear formulas; that is reported as not proved by Lean, not as wrong.

What carries a result from the working rows and the core to the whole table is proved once, for every run,
in the theorem library `lean/Neo` (cited per form in `LIBRARY`). Row data stays out of the LLM: the Lean
source is kept for people only and never placed in a certificate.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from fractions import Fraction
from typing import Dict, List, Optional

import z3

CHUNK = 25          # facts per theorem: one long conjunction overflows Lean's elaborator

# the general theorems each form relies on, proved once in lean/Neo
LIBRARY = {
    "value": [("Neo.Exact.unique_of_unsat", "Z3's `ans = t ∧ ans ≠ v` is unsat, so t = v"),
              ("Neo.Exact.sum_const_den", "SUM(numerator) / denominator is the exact sum of the fractions"),
              ("Neo.Exact.cmp_sign", "comparing numerator × denominator signs with 0 compares the fractions")],
    "table": [("Neo.Exact.sum_const_den", "SUM(numerator) / denominator is the exact sum of the fractions"),
              ("Neo.Exact.cmp_sign", "comparing numerator × denominator signs with 0 compares the fractions")],
    "extremum": [("Neo.Extremum.ranking_complete", "equal counts of rows beating the value prove the ranking"),
                 ("Neo.Exact.cmp_sign", "the 'strictly better' comparison is exact")],
    "set": [("Neo.Exact.cmp_sign", "the membership conditions compare exactly")],
    "check": [("Neo.Counterexample.holds_of_zero", "zero counterexamples: the claim is never false on a row"),
              ("Neo.Counterexample.refuted_of_pos", "a positive count is a real counterexample")],
    "check_global": [("Neo.Refinement.bound_of_core", "what follows from the core rows follows from the working rows"),
                     ("Neo.Refinement.unsat_of_subset", "unsat on the working rows is unsat on every row")],
    "feasible": [("Neo.Refinement.sat_mono", "the witness was checked on every row by the lake")],
    "infeasible": [("Neo.Refinement.bound_of_core", "a clash among the core rows is a clash among the working rows"),
                   ("Neo.Refinement.unsat_of_subset", "a clash on the working rows is a clash on every row"),
                   ("Neo.Clash.in_every_clash", "a constraint whose removal opens a route is in every clash")],
    "optimal_max": [("Neo.Refinement.bound_of_core", "the bound from the core rows holds on the working rows"),
                    ("Neo.Refinement.optimal_of_subset",
                     "bounded on the working rows + attained on every row = the maximum over every row")],
    "optimal_min": [("Neo.Refinement.bound_of_core", "the bound from the core rows holds on the working rows"),
                    ("Neo.Refinement.minimal_of_subset",
                     "bounded on the working rows + attained on every row = the minimum over every row")],
}


class Unsupported(ValueError):
    pass


# ---------------------------------------------------------------------- Z3 term -> Lean text
def ident(name: str) -> str:
    return "v_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def lean_sort(sort) -> str:
    k = sort.kind()
    if k == z3.Z3_REAL_SORT:
        return "Rat"
    if k == z3.Z3_INT_SORT:
        return "Int"
    if k == z3.Z3_BOOL_SORT:
        return "Bool"
    raise Unsupported(f"sort {sort}")


def number(v: Fraction, sort: str) -> str:
    if v.denominator == 1:
        return f"({v.numerator} : {sort})"
    if sort != "Rat":
        raise Unsupported("a fraction in an integer position")
    return f"(({v.numerator} : Rat) / {v.denominator})"


def literal(v, sort: str) -> str:
    if sort == "Bool":
        return "true" if v else "false"
    return number(Fraction(v), sort)


class LeanText:
    """Renders a Z3 term as Lean source. `env` replaces variables by values (to state a fact about a witness)."""

    OPS = {z3.Z3_OP_LE: "≤", z3.Z3_OP_LT: "<", z3.Z3_OP_GE: "≥", z3.Z3_OP_GT: ">"}

    def __init__(self, env: Optional[Dict[str, object]] = None):
        self.env = env or {}
        self.vars: Dict[str, str] = {}          # variable name -> Lean sort, as met

    def prop(self, t) -> str:
        """A Z3 Bool as a Lean proposition."""
        if z3.is_true(t):
            return "True"
        if z3.is_false(t):
            return "False"
        if z3.is_const(t) and t.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            return f"({self.term(t)} = true)"
        k, a = t.decl().kind(), t.children()
        if k == z3.Z3_OP_AND:
            return "(" + " ∧ ".join(self.prop(x) for x in a) + ")"
        if k == z3.Z3_OP_OR:
            return "(" + " ∨ ".join(self.prop(x) for x in a) + ")"
        if k == z3.Z3_OP_NOT:
            return f"(¬ {self.prop(a[0])})"
        if k == z3.Z3_OP_IMPLIES:
            return f"({self.prop(a[0])} → {self.prop(a[1])})"
        if k == z3.Z3_OP_ITE:
            return f"(if {self.prop(a[0])} then {self.prop(a[1])} else {self.prop(a[2])})"
        if k == z3.Z3_OP_EQ:
            if z3.is_bool(a[0]):
                return f"({self.prop(a[0])} ↔ {self.prop(a[1])})"
            return f"({self.term(a[0])} = {self.term(a[1])})"
        if k == z3.Z3_OP_DISTINCT:
            if len(a) != 2:
                raise Unsupported("distinct over more than two terms")
            return f"({self.term(a[0])} ≠ {self.term(a[1])})"
        if k in self.OPS:
            return f"({self.term(a[0])} {self.OPS[k]} {self.term(a[1])})"
        raise Unsupported(f"condition `{t.decl().name()}`")

    def term(self, t) -> str:
        """A Z3 Int or Real term as a Lean `Int` or `Rat` expression."""
        sort = lean_sort(t.sort())
        if z3.is_int_value(t):
            return number(Fraction(t.as_long()), sort)
        if z3.is_rational_value(t):
            return number(Fraction(t.numerator_as_long(), t.denominator_as_long()), sort)
        if z3.is_const(t) and t.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            name = t.decl().name()
            if name in self.env:
                return literal(self.env[name], sort)
            self.vars[name] = sort
            return ident(name)
        if z3.is_bool(t):
            return f"(decide {self.prop(t)})"
        k, a = t.decl().kind(), t.children()
        if k == z3.Z3_OP_ADD:
            return "(" + " + ".join(self.term(x) for x in a) + ")"
        if k == z3.Z3_OP_SUB:
            return "(" + " - ".join(self.term(x) for x in a) + ")"
        if k == z3.Z3_OP_MUL:
            return "(" + " * ".join(self.term(x) for x in a) + ")"
        if k == z3.Z3_OP_UMINUS:
            return f"(-{self.term(a[0])})"
        if k == z3.Z3_OP_DIV:
            # Z3 leaves x / 0 unspecified, Lean defines it as 0: only divide by a non-zero constant
            d = z3.simplify(a[1])
            if not (z3.is_rational_value(d) or z3.is_int_value(d)) or z3.is_true(z3.simplify(d == 0)):
                raise Unsupported("division by a term that is not a non-zero constant")
            return f"({self.term(a[0])} / {self.term(a[1])})"
        if k == z3.Z3_OP_TO_REAL:
            if z3.is_int_value(a[0]):
                return number(Fraction(a[0].as_long()), "Rat")
            return f"(({self.term(a[0])} : Int) : Rat)"
        if k == z3.Z3_OP_ITE:
            return f"(if {self.prop(a[0])} then {self.term(a[1])} else {self.term(a[2])})"
        raise Unsupported(f"operation `{t.decl().name()}`")


def conjuncts(f) -> List:
    """A conjunction split into its parts, so each row of an `all_rows` becomes its own fact."""
    if z3.is_and(f):
        return [c for x in f.children() for c in conjuncts(x)]
    return [f]


# ---------------------------------------------------------------------- goals
class Goals:
    """Theorems one run asks Lean to check."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("lean", {})
        self.items: List[dict] = []
        self.cites: List[str] = []
        self.skipped: List[str] = []

    def cite(self, key: str):
        if key not in self.cites:
            self.cites.append(key)

    def _name(self, base: str) -> str:
        n = re.sub(r"[^A-Za-z0-9_]", "_", base)
        taken = {g["name"] for g in self.items}
        out, i = n, 2
        while out in taken:
            out, i = f"{n}_{i}", i + 1
        return out

    def compute(self, name: str, about: str, facts: List, env: Optional[dict] = None, total: Optional[int] = None):
        """Facts about concrete numbers (`facts` are Z3 Bools; `env` substitutes a witness for the unknowns)."""
        limit = int(self.cfg.get("max_row_facts", 2000))
        total = len(facts) if total is None else total
        try:
            w = LeanText(env)
            props = [w.prop(f) for f in facts[:limit]]
            if w.vars:
                raise Unsupported(f"unknowns without a value: {', '.join(sorted(w.vars))}")
        except (Unsupported, z3.Z3Exception) as e:
            self.skipped.append(f"{about}: {e}")
            return
        for k in range(0, max(1, len(props)), CHUNK):
            part = props[k:k + CHUNK] or ["True"]
            label = about if len(props) <= CHUNK else f"{about} (facts {k + 1}-{k + len(part)} of {total})"
            self.items.append({"name": self._name(name if len(props) <= CHUNK else f"{name}_{k // CHUNK + 1}"),
                               "kind": "compute", "about": label, "facts": len(part),
                               "statement": " ∧\n    ".join(part), "tactic": "decide +kernel"})
        if total > len(props):
            self.skipped.append(f"{about}: Lean checked the first {len(props):,} of {total:,} facts "
                                f"(lean.max_row_facts); the lake checked every row")

    def symbolic(self, name: str, about: str, hyps: List, concl):
        """For every value of the unknowns: hyps → concl (concl a Z3 Bool, or None for False)."""
        if len(hyps) > int(self.cfg.get("max_core", 60)):
            self.skipped.append(f"{about}: the core has {len(hyps)} formulas (lean.max_core)")
            return
        try:
            w = LeanText()
            hs = [w.prop(h) for h in hyps]
            c = w.prop(concl) if concl is not None else "False"
        except (Unsupported, z3.Z3Exception) as e:
            self.skipped.append(f"{about}: {e}")
            return
        names = sorted(w.vars)
        binders = " ".join(f"({ident(n)} : {w.vars[n]})" for n in names)
        body = " →\n    ".join(hs + [c])
        statement = f"∀ {binders},\n    {body}" if binders else body
        intro = "intro " + " ".join([ident(n) for n in names] + [f"h{i}" for i in range(len(hs))]) \
            if names or hs else ""
        self.items.append({"name": self._name(name), "kind": "symbolic", "about": about, "facts": len(hs),
                           "statement": statement, "tactic": f"{intro}; grind" if intro else "grind"})


THEOREMS = sorted({n for v in LIBRARY.values() for n, _ in v} | {
    "Neo.Exact.frac_sub", "Neo.Exact.cmp_const_den", "Neo.Exact.decimal_exact", "Neo.Units.scale_cmp",
    "Neo.Units.scale_sum", "Neo.Units.rescale", "Neo.Extremum.subset_of_length", "Neo.Counterexample.fails_iff",
    "Neo.Clash.clash_without", "Neo.Refinement.sat_mono"})
STANDARD_AXIOMS = ("propext", "Classical.choice", "Quot.sound")


# ---------------------------------------------------------------------- running Lean
def build(cfg: dict) -> dict:
    """`lake build` the theorem library, then list each theorem's axioms: only Lean's three standard ones
    (no `sorryAx`, no compiled-code shortcut) means every theorem is fully proved."""
    lc = dict(cfg.get("lean", {}), enabled="auto")
    tc = toolchain({"lean": lc})
    if tc is None or not tc["lake"]:
        return {"ok": False, "output": "Lean is not installed (see lean/README.md)", "axioms": {}}
    p = subprocess.run([tc["lake"], "build"], cwd=tc["project"], capture_output=True, text=True,
                       timeout=int(lc.get("timeout_seconds", 120)) * 5)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        return {"ok": False, "output": out, "axioms": {}}
    with tempfile.TemporaryDirectory(prefix="neo-lean-") as d:
        path = os.path.join(d, "Audit.lean")
        with open(path, "w") as fh:
            fh.write("import Neo\n" + "".join(f"#print axioms {n}\n" for n in THEOREMS))
        a = subprocess.run([tc["lake"], "env", "lean", path], cwd=tc["project"], capture_output=True, text=True,
                           timeout=int(lc.get("timeout_seconds", 120)))
    audit = (a.stdout or "") + (a.stderr or "")
    axioms = {}
    for n in THEOREMS:
        short = n.split(".")[-1]
        m = re.search(rf"'{re.escape(n)}' depends on axioms: \[([^\]]*)\]", audit) or \
            re.search(rf"'{re.escape(short)}' depends on axioms: \[([^\]]*)\]", audit)
        axioms[n] = [x.strip() for x in m.group(1).split(",")] if m else \
            ([] if f"'{n}' does not depend on any axioms" in audit else None)
    ok = a.returncode == 0 and all(v is not None and set(v) <= set(STANDARD_AXIOMS) for v in axioms.values())
    return {"ok": ok, "output": out + audit, "axioms": axioms, "version": version(tc)}


def toolchain(cfg: dict) -> Optional[dict]:
    """The Lean tools and theorem library to use, or None when Lean is not installed."""
    lc = cfg.get("lean", {})
    if str(lc.get("enabled", "auto")).lower() in ("off", "false", "no", "0"):
        return None
    elan = os.path.expanduser("~/.elan/bin")
    lean = lc.get("binary") or shutil.which("lean") or (os.path.join(elan, "lean")
                                                        if os.path.exists(os.path.join(elan, "lean")) else None)
    if not lean:
        return None
    lake = shutil.which("lake") or os.path.join(os.path.dirname(lean), "lake")
    project = lc.get("project")
    built = bool(project) and library_built(project)
    return {"lean": lean, "lake": lake if os.path.exists(lake) else None, "project": project, "built": built}


def library_built(project: str) -> bool:
    """The library's compiled files exist and are newer than its sources."""
    olean = os.path.join(project, ".lake", "build", "lib", "lean", "Neo.olean")
    if not os.path.exists(olean):
        return False
    t = os.path.getmtime(olean)
    for root, _, files in os.walk(os.path.join(project, "Neo")):
        for f in files:
            if f.endswith(".lean") and os.path.getmtime(os.path.join(root, f)) > t:
                return False
    return True


def version(tc: dict) -> str:
    try:
        out = subprocess.run([tc["lean"], "--version"], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"version ([0-9][^,\s]*)", out)
        return f"Lean {m.group(1)}" if m else "Lean"
    except (OSError, subprocess.SubprocessError):
        return "lean"


def source(goals: Goals, header: str, use_library: bool) -> tuple:
    """The Lean file and, per theorem, its first and last line."""
    L = (["import Neo"] if use_library else []) + ["/-!", *[f"  {x}" for x in header.splitlines()], "-/",
                                                    "set_option maxRecDepth 20000", ""]
    spans = {}
    for g in goals.items:
        start = len(L) + 1
        L += f"/-- {g['about']} -/\ntheorem {g['name']} :\n    {g['statement']} := by\n  {g['tactic']}\n".split("\n")
        spans[g["name"]] = (start, len(L))
    if use_library:
        for key in goals.cites:
            for name, _ in LIBRARY.get(key, []):
                L.append(f"#check @{name}")
    for g in goals.items:
        L.append(f"#print axioms {g['name']}")
    return "\n".join(L) + "\n", spans


def check(goals: Goals, cfg: dict, header: str) -> dict:
    """Run Lean on the goals. Status: checked | partial | failed | not_checked | library_only."""
    lc = cfg.get("lean", {})
    cites = [{"theorem": n, "says": s} for key in goals.cites for n, s in LIBRARY.get(key, [])]
    tc = toolchain(cfg)
    base = {"cites": cites, "skipped": goals.skipped}
    if tc is None:
        why = "Lean is turned off (lean.enabled)" if str(lc.get("enabled", "auto")).lower() in \
            ("off", "false", "no", "0") else "Lean is not installed (see lean/README.md)"
        if str(lc.get("enabled", "auto")).lower() == "required" and "not installed" in why:
            raise RuntimeError("lean.enabled is `required` but Lean is not installed")
        return {**base, "status": "not_checked", "reason": why, "theorems": []}
    library = "built" if tc["built"] else "not built (run `neo lean build`)"
    if not goals.items:
        return {**base, "status": "library_only", "library": library, "theorems": [],
                "reason": "this form needs no per-run theorem: its steps are lake counts covered by the library"}
    use_library = tc["built"] and tc["lake"] is not None
    text, spans = source(goals, header, use_library)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="neo-lean-") as d:
        path = os.path.join(d, "Run.lean")
        with open(path, "w") as fh:
            fh.write(text)
        cmd = [tc["lake"], "env", "lean", path] if use_library else [tc["lean"], path]
        try:
            p = subprocess.run(cmd, cwd=tc["project"] if use_library else d, capture_output=True, text=True,
                               timeout=int(lc.get("timeout_seconds", 120)))
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return {**base, "status": "not_checked", "reason": f"Lean took longer than "
                    f"{lc.get('timeout_seconds', 120)} s", "source": text, "library": library,
                    "theorems": [{**_public(g), "result": "not checked"} for g in goals.items]}
    errors: Dict[str, List[str]] = {}
    for m in re.finditer(r"^[^\n]*?:(\d+):\d+: error[^\n]*(?:\n(?![^\n]*:\d+:\d+: )[^\n]*)*", out, re.M):
        line = int(m.group(1))
        owner = next((n for n, (a, b) in spans.items() if a <= line <= b), "_file")
        errors.setdefault(owner, []).append(m.group(0).split(": error", 1)[-1].strip()[:400])
    axioms = {}
    for m in re.finditer(r"'([A-Za-z0-9_]+)' depends on axioms: \[([^\]]*)\]", out):
        axioms[m.group(1)] = [a.strip() for a in m.group(2).split(",") if a.strip()]
    theorems = []
    for g in goals.items:
        bad = [a for a in axioms.get(g["name"], []) if a not in ("propext", "Classical.choice", "Quot.sound")]
        if g["name"] in errors or bad or "_file" in errors:
            msg = (errors.get(g["name"]) or errors.get("_file") or [f"uses axioms {bad}"])[0]
            result = "rejected" if g["kind"] == "compute" else "not proved by Lean"
        else:
            msg, result = None, "checked"
        theorems.append({**_public(g), "result": result, "error": msg, "axioms": axioms.get(g["name"])})
    rejected = any(t["result"] == "rejected" for t in theorems)
    open_ = any(t["result"] != "checked" for t in theorems)
    return {**base, "status": "failed" if rejected else ("partial" if open_ or goals.skipped else "checked"),
            "lean": version(tc), "library": library, "imported_library": use_library,
            "seconds": round(time.time() - t0, 2), "theorems": theorems, "source": text}


def _public(g: dict) -> dict:
    return {k: g[k] for k in ("name", "kind", "about", "facts", "tactic")}


def summary(lean: dict) -> dict:
    """One verification step for the certificate: counts and statuses only, no row values."""
    th = lean.get("theorems") or []
    ok = sum(1 for t in th if t["result"] == "checked")
    facts = sum(t["facts"] for t in th if t["result"] == "checked")
    status = lean.get("status")
    if status in ("not_checked", "library_only"):
        result = {"not_checked": "not checked", "library_only": "library theorems only"}[status]
        detail = lean.get("reason")
    else:
        result = {"checked": "proved", "partial": "partly proved", "failed": "REJECTED"}[status]
        detail = f"{ok} of {len(th)} theorem(s), {facts:,} fact(s), kernel-checked"
    names = ", ".join(c["theorem"] for c in lean.get("cites", []))
    return {"step": "Lean kernel check of what Z3 decided",
            "method": "theorems about the exact formulas Z3 was given, checked by Lean's kernel"
                      + (f"; extended to every row by {names}" if names else ""),
            "result": result, "detail": detail}
