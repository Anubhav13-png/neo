"""The validator: every check a model must pass before any query runs.

Beyond syntax and names it checks what goes wrong in enterprise analytics:
  joins      every join is an ontology relation, walked in a valid direction
  fan-out    a one-to-many join multiplies rows: summing the parent's fields would double count
  units      thousands vs billions, percent vs ratio, dollars vs counts
  time       year-to-date fields, adding a level (stock) to an amount over a period (flow)
  access     every field and entity within the user's clearance; row policies attached per alias
  rules      rule_param() only from approved rules in force
  grounding  every triggered `block` constraint addressed (applied, with how, or not applicable, with why)
  cost       tables scanned vs the approval threshold
Errors are written so the LLM can fix them; access denials are also reported separately for the audit log.
"""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional, Set, Tuple

from ..dsl import AGGREGATES, PARAM_ONLY, SCALARS, ExprError, arity_error, parse, refs
from ..dsl.parse import Expr, ref_name
from ..ontology.model import CLEARANCE, Field, Ontology
from ..ontology.service import triggered_block_constraints
from ..util import is_power_of_ten, to_fraction
from .model import FORMS, SORTS, JoinSpec, Named, Param, Scope, SemanticModel, Universe, Var
from .units import SCALAR, UNKNOWN, Unit, combine, parse_unit

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
VAR_FORMS = ("feasibility", "optimisation", "check")


@dataclass
class ValidationContext:
    onto: Ontology
    question: str
    principal: Any                      # governance.identity.Principal
    rules: Any                          # rules.RuleRegistry
    catalog: Any = None
    snapshot: Any = None
    cfg: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    model: Optional[SemanticModel]
    errors: List[str]
    warnings: List[str]
    access_denied: List[str]


@dataclass
class T:
    kind: str                            # number | text | date | bool | null | any
    unit: Optional[Unit] = None
    time: Optional[str] = None
    literal: Optional[Fraction] = None
    aliases: frozenset = frozenset()
    has_var: bool = False
    has_agg: bool = False


ANY = T("any", UNKNOWN)


@dataclass
class Mode:
    where: str
    row: bool
    allow_agg: bool
    allow_vars: bool
    in_agg: bool = False
    param_ok: bool = False               # rule_param allowed


# ---------------------------------------------------------------------- unit algebra
def add_units(a: T, b: T) -> Tuple[Optional[Unit], Optional[str]]:
    ua, ub = a.unit or UNKNOWN, b.unit or UNKNOWN
    if ua.unknown:
        return ub, None
    if ub.unknown:
        return ua, None
    if a.literal is not None and ua == SCALAR:
        return ub, None
    if b.literal is not None and ub == SCALAR:
        return ua, None
    if ua.dim != ub.dim:
        return None, f"{ua.label()} and {ub.label()} are different quantities"
    if ua.scale != ub.scale:
        return None, (f"{ua.label()} and {ub.label()} differ by a factor of 10^{abs(ua.scale - ub.scale)}; "
                      f"convert one first (multiply or divide by a power of ten)")
    return ua, None


def mul_units(a: T, b: T) -> Unit:
    ua, ub = a.unit or UNKNOWN, b.unit or UNKNOWN
    if ua.unknown or ub.unknown:
        return UNKNOWN
    for lit, u_other, u_lit in ((b.literal, ua, ub), (a.literal, ub, ua)):
        if lit is not None and u_lit == SCALAR:
            e = is_power_of_ten(abs(lit)) if lit != 0 else None
            return Unit(u_other.dim, u_other.scale - e) if e is not None else u_other
    return Unit(combine(ua.dim, ub.dim, divide=False), ua.scale + ub.scale)


def div_units(a: T, b: T) -> Tuple[Unit, Optional[str]]:
    ua, ub = a.unit or UNKNOWN, b.unit or UNKNOWN
    if ua.unknown or ub.unknown:
        return UNKNOWN, None
    if b.literal is not None and ub == SCALAR:
        e = is_power_of_ten(abs(b.literal)) if b.literal != 0 else None
        return (Unit(ua.dim, ua.scale + e) if e is not None else ua), None
    if a.literal is not None and ua == SCALAR:
        return Unit(combine(None, ub.dim, divide=True), -ub.scale), None
    if ua.dim == ub.dim and ua.dim is not None and ua.scale != ub.scale:
        return SCALAR, (f"dividing {ua.label()} by {ub.label()} gives a ratio off by 10^{ua.scale - ub.scale}; "
                        f"bring both to the same scale first")
    return Unit(combine(ua.dim, ub.dim, divide=True), ua.scale - ub.scale), None


# ---------------------------------------------------------------------- the validator
class _V:
    def __init__(self, raw: dict, ctx: ValidationContext):
        self.raw, self.ctx, self.onto = raw, ctx, ctx.onto
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.denied: List[str] = []
        self.used: Dict[Tuple[str, str], Tuple[str, Field]] = {}
        self.unit_mode = ctx.cfg.get("governance", {}).get("unit_errors", "error")
        self.define_types: Dict[str, T] = {}
        self._typing: Set[str] = set()
        self.param_types: Dict[str, T] = {}

    def err(self, msg: str):
        if msg not in self.errors:
            self.errors.append(msg)

    def unit_problem(self, msg: str):
        (self.err if self.unit_mode == "error" else self.warnings.append)(msg)

    def parse(self, label: str, text) -> Optional[Expr]:
        try:
            return parse(text)
        except ExprError as e:
            self.err(f"{label}: {e}")
            return None

    # ------------------------------------------------------------------ universe
    def universe(self) -> Optional[Universe]:
        u = self.raw.get("universe")
        if not isinstance(u, dict) or not u.get("entity"):
            self.err("`universe` must be {\"entity\": <entity id>, \"joins\": [...]} naming the rows the model ranges over.")
            return None
        ent = u["entity"]
        if ent not in self.onto.entities:
            close = difflib.get_close_matches(str(ent), list(self.onto.entities), n=3, cutoff=0.5)
            self.err(f"universe.entity `{ent}` is not an ontology entity." + (f" Did you mean {', '.join(close)}?" if close else ""))
            return None
        self.check_entity_access(ent)
        uni = Universe(ent)
        aliases = {"": ent}
        for j in u.get("joins") or []:
            if not isinstance(j, dict) or not j.get("relation") or not j.get("as"):
                self.err("each universe join must be {\"relation\": <relation id>, \"as\": <alias>, \"from\": <alias, default root>}.")
                continue
            rel = self.onto.relations.get(j["relation"])
            if rel is None or rel.kind != "join":
                self.err(f"universe join `{j['relation']}` is not a join relation in the ontology.")
                continue
            alias, frm = j["as"], j.get("from", "") or ""
            if not _IDENT.match(alias) or alias in aliases or self.onto.field(ent, alias):
                self.err(f"join alias `{alias}` must be a new identifier that is not a field of `{ent}`.")
                continue
            if frm not in aliases:
                self.err(f"join `{alias}`: from-alias `{frm}` is not defined before it.")
                continue
            d = self.onto.join_direction(rel, aliases[frm], bool(j.get("reverse")))
            if d is None:
                self.err(f"join `{alias}`: relation `{rel.id}` does not connect `{aliases[frm]}` "
                         f"({rel.from_entity} -> {rel.to_entity}).")
                continue
            target, pairs, card = d
            self.check_entity_access(target)
            if card in ("one_to_many", "many_to_many"):
                uni.fanned |= set(aliases)
            uni.joins.append(JoinSpec(alias, rel.id, target, frm, pairs, card))
            aliases[alias] = target
        grain = u.get("grain")
        if uni.fanned:
            child = [j.alias for j in uni.joins if j.cardinality in ("one_to_many", "many_to_many")]
            if grain is None:
                self.err(f"join(s) {', '.join(child)} are one-to-many: each row of "
                         f"{', '.join(a or 'the root' for a in sorted(uni.fanned))} is repeated once per joined row. "
                         f"Set universe.grain to `{child[-1]}` to confirm rows are now one per `{child[-1]}`, and do not "
                         f"sum or average fields of the repeated side (they would be double counted).")
            elif grain not in aliases or grain in uni.fanned:
                self.err(f"universe.grain `{grain}` must be the alias whose rows are not repeated ({', '.join(child)}).")
        uni.grain = grain
        ss = u.get("scenario_set")
        if ss:
            s = self.onto.scenario_sets.get(ss)
            if s is None or s.entity != ent:
                self.err(f"universe.scenario_set `{ss}` is not a scenario set of `{ent}`.")
            else:
                uni.scenario_set = ss
        return uni

    def check_entity_access(self, ent: str):
        e = self.onto.entities[ent]
        if CLEARANCE.index(e.classification) > CLEARANCE.index(self.ctx.principal.clearance):
            msg = f"access denied: entity `{ent}` is {e.classification}; your clearance is {self.ctx.principal.clearance}"
            self.err(msg)
            self.denied.append(msg)

    # ------------------------------------------------------------------ typing
    def typ(self, expr: Optional[Expr], mode: Mode) -> T:
        if expr is None:
            return ANY
        try:
            return self.node(expr.body, mode, expr.src)
        except _Stop:
            return ANY

    def node(self, n, m: Mode, src: str) -> T:
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool):
                return T("bool")
            if isinstance(v, (int, float)):
                return T("number", SCALAR, literal=to_fraction(v))
            if isinstance(v, str):
                return T("text")
            return T("null")
        if isinstance(n, (ast.Name, ast.Attribute)):
            return self.name(ref_name(n), m, src)
        if isinstance(n, ast.UnaryOp):
            t = self.node(n.operand, m, src)
            if isinstance(n.op, ast.Not):
                self.want(t, "bool", m, src, "`not` needs a condition")
                return T("bool", has_var=t.has_var, has_agg=t.has_agg, aliases=t.aliases)
            self.want(t, "number", m, src, "unary minus needs a number")
            lit = -t.literal if (t.literal is not None and isinstance(n.op, ast.USub)) else t.literal
            return T("number", t.unit, t.time, lit, t.aliases, t.has_var, t.has_agg)
        if isinstance(n, ast.BoolOp):
            ts = [self.node(v, m, src) for v in n.values]
            for t in ts:
                self.want(t, "bool", m, src, "`and` / `or` need conditions")
            return T("bool", aliases=_union(ts), has_var=any(t.has_var for t in ts), has_agg=any(t.has_agg for t in ts))
        if isinstance(n, ast.BinOp):
            return self.binop(n, m, src)
        if isinstance(n, ast.Compare):
            return self.compare(n, m, src)
        if isinstance(n, ast.IfExp):
            c = self.node(n.test, m, src)
            self.want(c, "bool", m, src, "the condition of `x if c else y`")
            a, b = self.node(n.body, m, src), self.node(n.orelse, m, src)
            return self.merge(a, b, m, src, "the two branches of `x if c else y`", extra=[c])
        if isinstance(n, ast.Call):
            return self.call(n, m, src)
        if isinstance(n, (ast.List, ast.Tuple)):
            self.err(f"{m.where}: a list is only allowed on the right of `in` (in `{src}`).")
            raise _Stop()
        self.err(f"{m.where}: unsupported expression in `{src}`.")
        raise _Stop()

    def want(self, t: T, kind: str, m: Mode, src: str, what: str):
        if t.kind in ("any", kind) or (kind == "text" and t.kind == "date"):
            return
        self.err(f"{m.where}: {what}, got {t.kind} (in `{src}`).")

    def name(self, name: str, m: Mode, src: str) -> T:
        scope = self.scope
        kind = scope.kind(name)
        if kind == "param":
            return self.param_types.get(name, ANY)
        if kind == "var":
            if not m.allow_vars:
                self.err(f"{m.where}: variable `{name}` cannot be used here (in `{src}`); variables belong in "
                         f"constraints, assumptions, objectives and global claims.")
            v = scope.variables[name]
            return T("bool" if v.sort == "Bool" else "number", v.unit or UNKNOWN, has_var=True)
        if kind in ("define", "field"):
            if not m.row:
                self.err(f"{m.where}: `{name}` is a per-row value; outside a per-row expression wrap it in an "
                         f"aggregate such as sum({name}) or max_of({name}) (in `{src}`).")
                raise _Stop()
            if kind == "define":
                t = self.define_type(name)
                if t.has_var and not m.allow_vars:
                    self.err(f"{m.where}: `{name}` uses a variable, which is not allowed here (in `{src}`).")
                return t
            alias, f = scope.field(name)
            return self.field_type(alias, f, m, src)
        close = difflib.get_close_matches(name, self.known_names(), n=3, cutoff=0.6)
        self.err(f"{m.where}: unknown name `{name}` in `{src}`." + (f" Did you mean: {', '.join(close)}?" if close else "")
                 + " Names are fields of the universe entity, `alias.field` for joined entities, defines, parameters "
                   "or variables.")
        raise _Stop()

    def known_names(self) -> List[str]:
        s = self.scope
        out = list(s.defines) + list(s.params) + list(s.variables)
        if s.universe:
            out += [f.name for f in self.onto.fields_of(s.universe.entity)]
            for j in s.universe.joins:
                out += [f"{j.alias}.{f.name}" for f in self.onto.fields_of(j.entity)]
        return out

    def field_type(self, alias: str, f: Field, m: Mode, src: str) -> T:
        self.used[(alias, f.name)] = (alias, f)
        if CLEARANCE.index(f.classification) > CLEARANCE.index(self.ctx.principal.clearance):
            msg = (f"access denied: field `{f.entity}.{f.name}` is {f.classification}; your clearance is "
                   f"{self.ctx.principal.clearance}")
            self.err(msg)
            if msg not in self.denied:
                self.denied.append(msg)
        if f.time_semantics == "ytd":
            self.err(f"{m.where}: `{f.name}` is year-to-date (it accumulates from January), so it is not one period's "
                     f"amount; use `{f.per_period}` (in `{src}`).")
        try:
            unit = parse_unit(f.unit)
        except ValueError:
            unit = UNKNOWN
        kind = f.dtype or "number"
        if unit is None and kind == "number":
            unit = UNKNOWN
        return T(kind, unit if kind == "number" else None, f.time_semantics, aliases=frozenset({alias}))

    def define_type(self, name: str) -> T:
        if name in self.define_types:
            return self.define_types[name]
        if name in self._typing:
            self.err(f"define `{name}` refers to itself.")
            raise _Stop()
        self._typing.add(name)
        try:
            t = self.typ(self.scope.defines[name], Mode(f"define `{name}`", True, False, self.form in VAR_FORMS))
        finally:
            self._typing.discard(name)
        self.define_types[name] = t
        return t

    def binop(self, n: ast.BinOp, m: Mode, src: str) -> T:
        a, b = self.node(n.left, m, src), self.node(n.right, m, src)
        for t in (a, b):
            self.want(t, "number", m, src, "arithmetic needs numbers")
        flags = dict(aliases=a.aliases | b.aliases, has_var=a.has_var or b.has_var, has_agg=a.has_agg or b.has_agg)
        if isinstance(n.op, (ast.Add, ast.Sub)):
            unit, problem = add_units(a, b)
            if problem:
                self.unit_problem(f"{m.where}: cannot add or subtract: {problem} (in `{src}`).")
            time = self.time_merge(a, b, m, src)
            lit = None
            if a.literal is not None and b.literal is not None:
                lit = a.literal + b.literal if isinstance(n.op, ast.Add) else a.literal - b.literal
            return T("number", unit or UNKNOWN, time, lit, **flags)
        if isinstance(n.op, ast.Mult):
            lit = a.literal * b.literal if a.literal is not None and b.literal is not None else None
            return T("number", mul_units(a, b), None if (a.time and b.time) else (a.time or b.time), lit, **flags)
        if isinstance(n.op, ast.Div):
            unit, problem = div_units(a, b)
            if problem:
                self.unit_problem(f"{m.where}: {problem} (in `{src}`).")
            lit = a.literal / b.literal if a.literal is not None and b.literal not in (None, 0) else None
            return T("number", unit, a.time if b.time is None else None, lit, **flags)
        if b.literal is None or b.literal <= 0 or b.literal.denominator != 1:
            self.err(f"{m.where}: `//` and `%` need a positive whole-number literal on the right (in `{src}`).")
        return T("number", a.unit, a.time, None, **flags)

    def time_merge(self, a: T, b: T, m: Mode, src: str) -> Optional[str]:
        if a.time and b.time and a.time != b.time:
            self.err(f"{m.where}: mixes a level at a point in time (stock) with an amount over a period (flow) "
                     f"(in `{src}`).")
        return a.time or b.time

    def merge(self, a: T, b: T, m: Mode, src: str, what: str, extra=()) -> T:
        ts = [a, b, *extra]
        flags = dict(aliases=_union(ts), has_var=any(t.has_var for t in ts), has_agg=any(t.has_agg for t in ts))
        if a.kind == "null":
            return T(b.kind, b.unit, b.time, **flags)
        if b.kind == "null":
            return T(a.kind, a.unit, a.time, **flags)
        if "any" not in (a.kind, b.kind) and {a.kind, b.kind} != {"text", "date"} and a.kind != b.kind:
            self.err(f"{m.where}: {what} have different types ({a.kind} vs {b.kind}) (in `{src}`).")
        unit = a.unit
        if a.kind == "number" and b.kind == "number":
            unit, problem = add_units(a, b)
            if problem:
                self.unit_problem(f"{m.where}: {what}: {problem} (in `{src}`).")
        return T(a.kind if a.kind != "any" else b.kind, unit, a.time or b.time, **flags)

    def compare(self, n: ast.Compare, m: Mode, src: str) -> T:
        left = self.node(n.left, m, src)
        ts = [left]
        for op, right_node in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                if not isinstance(right_node, (ast.List, ast.Tuple)) or \
                        not all(isinstance(e, ast.Constant) for e in right_node.elts):
                    self.err(f"{m.where}: `in` needs a list of literal values on the right (in `{src}`).")
                    continue
                for e in right_node.elts:
                    self.merge(left, self.node(e, m, src), m, src, "the value and the list")
                continue
            right = self.node(right_node, m, src)
            if "null" in (left.kind, right.kind):
                self.err(f"{m.where}: comparing with None is always false; use is_null(x) or not_null(x) (in `{src}`).")
            elif left.kind == "bool" and right.kind == "bool":
                pass
            else:
                self.merge(left, right, m, src, "the two sides of a comparison")
                self.time_merge(left, right, m, src)
            ts.append(right)
            left = right
        return T("bool", aliases=_union(ts), has_var=any(t.has_var for t in ts), has_agg=any(t.has_agg for t in ts))

    def call(self, n: ast.Call, m: Mode, src: str) -> T:
        name = n.func.id
        if n.keywords:
            self.err(f"{m.where}: keyword arguments are not supported (in `{src}`).")
            raise _Stop()
        problem = arity_error(name, len(n.args))
        if problem:
            self.err(f"{m.where}: {problem} (in `{src}`).")
            raise _Stop()
        if name in AGGREGATES:
            return self.aggregate(name, n, m, src)
        if name in PARAM_ONLY:
            return self.rule_param(n, m, src)
        args = [self.node(a, m, src) for a in n.args]
        flags = dict(aliases=_union(args), has_var=any(t.has_var for t in args), has_agg=any(t.has_agg for t in args))
        if name == "implies":
            for t in args:
                self.want(t, "bool", m, src, "implies() needs conditions")
            return T("bool", **flags)
        if name == "ite":
            self.want(args[0], "bool", m, src, "ite(c, x, y) needs a condition first")
            return self.merge(args[1], args[2], m, src, "the branches of ite()", extra=[args[0]])
        if name == "abs":
            self.want(args[0], "number", m, src, "abs() needs a number")
            return T("number", args[0].unit, args[0].time, **flags)
        if name in ("min", "max", "coalesce"):
            return self.merge(args[0], args[1], m, src, f"the arguments of {name}()")
        if name in ("contains", "startswith"):
            for t in args:
                self.want(t, "text", m, src, f"{name}() needs text")
            return T("bool", **flags)
        if name == "lower":
            self.want(args[0], "text", m, src, "lower() needs text")
            return T("text", **flags)
        if name == "year_of":
            self.want(args[0], "text", m, src, "year_of() needs a date")
            return T("number", Unit("years"), **flags)
        if name in ("is_null", "not_null"):
            return T("bool", **flags)
        self.err(f"{m.where}: unknown function `{name}()` (in `{src}`).")
        raise _Stop()

    def aggregate(self, name: str, n: ast.Call, m: Mode, src: str) -> T:
        if not m.allow_agg or m.row or m.in_agg:
            where = "a per-row expression" if m.row else "here"
            self.err(f"{m.where}: aggregate {name}() is not allowed in {where} (in `{src}`).")
            raise _Stop()
        inner = Mode(m.where, True, False, m.allow_vars, in_agg=True)
        args = [self.node(a, inner, src) for a in n.args]
        has_var = any(t.has_var for t in args)
        if name in ("count", "share", "any_row", "all_rows"):
            for t in args:
                self.want(t, "bool", inner, src, f"{name}() needs a condition")
            kind = "bool" if name in ("any_row", "all_rows") else "number"
            unit = None if kind == "bool" else (Unit("count") if name == "count" else SCALAR)
            return T(kind, unit, has_var=has_var, has_agg=True)
        val = args[0]
        if len(args) > 1:
            self.want(args[1], "bool", inner, src, f"the condition of {name}()")
        if name in ("sum", "avg"):
            self.want(val, "number", inner, src, f"{name}() needs numbers")
            fanned = set(val.aliases) & (self.uni.fanned if self.uni else set())
            if fanned:
                self.err(f"{m.where}: {name}() over fields of {', '.join(a or 'the root entity' for a in sorted(fanned))} "
                         f"would double count, because a one-to-many join repeats those rows (in `{src}`). Aggregate "
                         f"at the parent grain instead (a universe without that join), or use min_of / max_of.")
            if name == "sum" and val.time == "stock" and self.uni is not None:
                ent = self.onto.entities[self.uni.entity]
                if ent.time_field:
                    self.warnings.append(f"{m.where}: sum() of a level (stock) adds it across rows; that is only "
                                         f"meaningful within one point in time of `{ent.time_field}` (in `{src}`).")
        return T(val.kind, val.unit, val.time, has_var=has_var, has_agg=True)

    def rule_param(self, n: ast.Call, m: Mode, src: str) -> T:
        if not m.param_ok:
            self.err(f"{m.where}: rule_param() belongs in `parameters` (in `{src}`).")
            raise _Stop()
        if not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in n.args):
            self.err(f"{m.where}: rule_param('rule_id', 'name') needs two text literals (in `{src}`).")
            raise _Stop()
        rid, pname = n.args[0].value, n.args[1].value
        problem = self.ctx.rules.check_citable(rid) if self.ctx.rules else f"no rule registry"
        if problem:
            self.err(f"{m.where}: {problem} (in `{src}`).")
            raise _Stop()
        params = self.ctx.rules.get(rid).get("parameters") or {}
        if pname not in params:
            self.err(f"{m.where}: rule `{rid}` has no parameter `{pname}` (has: {', '.join(params)}).")
            raise _Stop()
        value = params[pname].get("value")
        if isinstance(value, str):
            return T("text")
        try:
            unit = parse_unit(params[pname].get("unit")) or UNKNOWN
        except ValueError:
            unit = UNKNOWN
        return T("number", unit)

    # ------------------------------------------------------------------ main
    def run(self) -> ValidationResult:
        raw, onto = self.raw, self.onto
        if not isinstance(raw, dict):
            return ValidationResult(None, ["The reply must be one JSON object (the model)."], [], [])
        self.form = form = raw.get("form")
        if form not in FORMS:
            self.err(f"`form` must be one of {', '.join(FORMS)}; got {form!r}.")
        interpretation = raw.get("interpretation")
        if not isinstance(interpretation, str) or len(interpretation.strip()) < 10:
            self.err("`interpretation` must restate precisely, in a sentence, what is being asked.")
        if form == "not_expressible":
            if self.errors:
                return ValidationResult(None, self.errors, self.warnings, self.denied)
            return ValidationResult(SemanticModel(raw, form, interpretation, None, None, {}, {}, None, [], [], [], [],
                                                  {}, [], None, raw.get("grounding") or {}), [], self.warnings, [])
        self.uni = uni = self.universe()
        defines_raw = raw.get("define") or {}
        params_raw = raw.get("parameters") or {}
        vars_raw = raw.get("variables") or []
        if not isinstance(defines_raw, dict) or not isinstance(params_raw, dict) or not isinstance(vars_raw, list):
            self.err("`define` and `parameters` must be objects, `variables` a list.")
            return ValidationResult(None, self.errors, self.warnings, self.denied)
        root_fields = {f.name for f in onto.fields_of(uni.entity)} if uni else set()
        taken = set(root_fields) | (set(uni.aliases) if uni else set())

        def fresh(kind, name):
            if not _IDENT.match(str(name)) or name in taken:
                self.err(f"{kind} `{name}`: must be an identifier that is not a field or alias of the universe, "
                         f"and not used twice.")
                return False
            taken.add(name)
            return True

        defines: Dict[str, Expr] = {}
        for name, text in defines_raw.items():
            if fresh("define", name):
                e = self.parse(f"define `{name}`", text)
                if e is not None:
                    defines[name] = e
        params: List[Param] = []
        for name, spec in params_raw.items():
            if not fresh("parameter", name):
                continue
            text, unit_text = (spec.get("expr"), spec.get("unit")) if isinstance(spec, dict) else (spec, None)
            e = self.parse(f"parameter `{name}`", text)
            if e is None:
                continue
            unit = None
            if unit_text is not None:
                try:
                    unit = parse_unit(unit_text)
                except ValueError as ex:
                    self.err(f"parameter `{name}`: {ex}")
            calls = refs(e)[1]
            stated = isinstance(e.body, ast.Constant) or (isinstance(e.body, ast.UnaryOp) and
                                                          isinstance(e.body.operand, ast.Constant))
            params.append(Param(name, e, unit, bool(calls & set(AGGREGATES)), stated))
        variables: List[Var] = []
        for v in vars_raw:
            if not isinstance(v, dict) or not fresh("variable", v.get("name")):
                if not isinstance(v, dict):
                    self.err("each variable must be {name, sort, domain}.")
                continue
            sort = v.get("sort", "Real")
            if sort not in SORTS:
                self.err(f"variable `{v['name']}`: sort must be one of {SORTS}.")
            dom = self.parse(f"variable `{v['name']}` domain", v["domain"]) if v.get("domain") else None
            unit = None
            if v.get("unit"):
                try:
                    unit = parse_unit(v["unit"])
                except ValueError as ex:
                    self.err(f"variable `{v['name']}`: {ex}")
            variables.append(Var(v["name"], sort, dom, unit))
        if variables and form not in VAR_FORMS:
            self.err(f"form `{form}` is computed from the data: `variables`, `constraints` and `assumptions` belong to "
                     f"check (scope global), feasibility and optimisation.")
        self.scope = Scope(onto, uni, defines, {p.name: p for p in params}, {v.name: v for v in variables})
        var_ok = form in VAR_FORMS

        # parameters, in order: each may use the ones before it
        visible: Dict[str, Param] = {}
        for p in params:
            self.scope.params = dict(visible)
            t = self.typ(p.expr, Mode(f"parameter `{p.name}`", False, True, False, param_ok=True))
            if t.kind == "number" and p.unit is not None:
                t = T("number", p.unit, t.time, t.literal)
            elif t.kind == "number" and p.stated:
                t = T("number", UNKNOWN, literal=t.literal)
            self.param_types[p.name] = t
            visible[p.name] = p
        self.scope.params = {p.name: p for p in params}
        for name in defines:
            self.define_type(name)
        policies: Dict[str, Expr] = {}
        if uni:
            for alias, ent in uni.aliases.items():
                text = self.ctx.principal.row_policies.get(ent)
                if text:
                    e = self.parse(f"row policy on `{ent}`", text)
                    if e is not None:
                        for nm in refs(e)[0]:
                            if not onto.field(ent, nm):
                                self.err(f"row policy on `{ent}` uses `{nm}`, which is not a field of `{ent}`; fix "
                                         f"config/users.yaml.")
                        policies[alias] = e
        rows = self.parse("rows", raw.get("rows", "True"))
        if uni and uni.scenario_set and rows is not None:
            ss = onto.scenario_sets[uni.scenario_set]
            rows = parse(f"({rows.src}) and ({ss.rows})")
        t = self.typ(rows, Mode("rows", True, False, False))
        self.want(t, "bool", Mode("rows", True, False, False), rows.src if rows else "", "`rows` must be a condition")
        group_by = self.parse("group_by", raw["group_by"]) if raw.get("group_by") else None
        if group_by is not None:
            gt = self.typ(group_by, Mode("group_by", True, False, False))
            if gt.kind == "number" and gt.literal is None:
                self.notes_group_numeric = True
        # aggregate parameters are computed over the eligible rows, so they may feed defines used in targets and
        # constraints, but not the filter or grouping that decides which rows are eligible (that is circular)
        tainted = {p.name for p in params if p.has_agg}
        changed = True
        while changed:
            changed = False
            for k, v in defines.items():
                if k not in tainted and refs(v)[0] & tainted:
                    tainted.add(k)
                    changed = True
        for label, e in [("rows", rows), ("group_by", group_by)]:
            if e is not None and refs(e)[0] & tainted:
                self.err(f"{label} uses {sorted(refs(e)[0] & tainted)}, which depend on aggregates over the eligible "
                         f"rows; that is circular. Filters and groupings may only use plain parameters.")
        for v in variables:
            if v.domain is not None:
                self.want(self.typ(v.domain, Mode(f"variable `{v.name}` domain", False, False, True)), "bool",
                          Mode(f"variable `{v.name}` domain", False, False, True), v.domain.src, "a domain is a condition")

        def named(key, label) -> List[Named]:
            out, seen = [], set()
            for item in raw.get(key) or []:
                if not isinstance(item, dict) or not item.get("name") or "expr" not in item:
                    self.err(f"each entry of `{key}` needs a `name` and an `expr`.")
                    continue
                if item["name"] in seen:
                    self.err(f"duplicate {label} name `{item['name']}`.")
                seen.add(item["name"])
                e = self.parse(f"{label} `{item['name']}`", item["expr"])
                if e is None:
                    continue
                mode = Mode(f"{label} `{item['name']}`", False, True, True)
                self.want(self.typ(e, mode), "bool", mode, e.src, f"a {label} must be a condition")
                src = item.get("source", "")
                if src.startswith("rule:") and self.ctx.rules:
                    problem = self.ctx.rules.check_citable(src[5:])
                    if problem:
                        self.err(f"{label} `{item['name']}`: {problem}.")
                out.append(Named(item["name"], e, src))
            return out

        constraints = named("constraints", "constraint")
        assumptions = named("assumptions", "assumption")
        if (constraints or assumptions) and form not in VAR_FORMS:
            self.err(f"form `{form}` takes no constraints or assumptions.")
        target = self.target(form, group_by)
        notes = {"target_units": target.pop("_units", {})}
        sensitivity = []
        plain = {p.name for p in params if not p.has_agg}
        for item in (raw.get("sensitivity") or [])[:3]:
            if not isinstance(item, dict) or item.get("parameter") not in plain:
                self.err("each `sensitivity` entry must be {\"parameter\": <a parameter without aggregates>, "
                         "\"values\": [numbers]}.")
                continue
            vals = item.get("values")
            if not isinstance(vals, list) or not 1 <= len(vals) <= 12 or \
                    not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
                self.err(f"sensitivity `{item['parameter']}`: `values` must be 1 to 12 numbers.")
                continue
            sensitivity.append({"parameter": item["parameter"], "values": vals})
        backtest = self.backtest(form, variables, params)
        grounding = raw.get("grounding") or {}
        self.grounding(grounding, uni)
        needs_approval = self.cost(uni)
        if self.errors:
            return ValidationResult(None, self.errors, self.warnings, self.denied)
        model = SemanticModel(raw, form, interpretation, uni, rows, policies, defines, group_by, params, variables,
                              constraints, assumptions, target, sensitivity, backtest, grounding,
                              list(self.used.values()), notes, needs_approval)
        model.notes["stated_parameters"] = [p.name for p in params if p.stated]
        model.notes["param_units"] = {k: (t.unit.label() if t.unit else None) for k, t in self.param_types.items()}
        model.notes["fanned"] = sorted(uni.fanned) if uni else []
        return ValidationResult(model, [], self.warnings, self.denied)

    def target(self, form: str, group_by) -> dict:
        tgt = dict(self.raw.get("target") or {})
        units = {}
        whole = lambda w: Mode(w, False, True, form in VAR_FORMS)
        if form in ("value", "table"):
            if form == "table" and group_by is None:
                self.err("form `table` needs `group_by`.")
            cols = tgt.get("columns")
            if form == "value" and not cols:
                cols = {"value": tgt.get("expr")} if tgt.get("expr") is not None else None
            if not isinstance(cols, dict) or not cols:
                self.err("the target needs `expr` (one value) or `columns`: {name: aggregate expression}.")
                return tgt
            parsed = {}
            for k, v in cols.items():
                if not _IDENT.match(str(k)):
                    self.err(f"target column `{k}` must be an identifier.")
                e = self.parse(f"target.columns.{k}", v)
                if e is not None:
                    t = self.typ(e, whole(f"target.columns.{k}"))
                    units[k] = t.unit.label() if t.unit else t.kind
                    parsed[k] = e
            tgt["columns"] = parsed
            tgt.pop("expr", None)
        elif form == "extremum":
            e = self.parse("target.expr", tgt.get("expr"))
            mode = whole("target.expr") if group_by is not None else Mode("target.expr", True, False, False)
            t = self.typ(e, mode)
            units["value"] = t.unit.label() if t.unit else t.kind
            tgt["expr"] = e
            if tgt.get("sense") not in ("max", "min"):
                self.err("extremum target needs `sense`: 'max' or 'min'.")
            try:
                tgt["top"] = max(1, min(100, int(tgt.get("top", 1))))
            except (TypeError, ValueError):
                self.err("extremum `top` must be a whole number.")
        elif form == "set":
            sel = tgt.get("select") or []
            if not isinstance(sel, list):
                self.err("set target `select` must be a list of fields.")
                sel = []
            for s in sel:
                if self.scope.kind(s) not in ("field", "define"):
                    self.err(f"set target select: `{s}` is not a field or define of the universe.")
                elif self.scope.kind(s) == "field":
                    self.typ(parse(s), Mode("target.select", True, False, False))
            tgt["where"] = self.parse("target.where", tgt["where"]) if tgt.get("where") else None
            if tgt["where"] is not None:
                self.typ(tgt["where"], Mode("target.where", True, False, False))
        elif form == "check":
            scope = tgt.get("scope", "rows")
            if scope not in ("rows", "global"):
                self.err("check target `scope` must be 'rows' or 'global'.")
            e = self.parse("target.claim", tgt.get("claim"))
            mode = Mode("target.claim", True, False, False) if scope == "rows" else whole("target.claim")
            self.want(self.typ(e, mode), "bool", mode, e.src if e else "", "a claim must be a condition")
            tgt["claim"], tgt["scope"] = e, scope
        elif form == "feasibility":
            if not self.raw.get("constraints") and not self.raw.get("assumptions"):
                self.err("form `feasibility` needs `constraints` (the rules) and usually `assumptions`.")
        elif form == "optimisation":
            e = self.parse("target.objective", tgt.get("objective"))
            t = self.typ(e, whole("target.objective"))
            units["objective"] = t.unit.label() if t.unit else t.kind
            tgt["objective"] = e
            if tgt.get("sense") not in ("max", "min"):
                self.err("optimisation target needs `sense`: 'max' or 'min'.")
        tgt["_units"] = units
        return tgt

    def backtest(self, form, variables, params) -> Optional[dict]:
        bt = self.raw.get("backtest")
        if not bt:
            return None
        if form not in ("feasibility", "optimisation") or not isinstance(bt, dict):
            self.err("`backtest` is only used with feasibility or optimisation, as {universe, rows, bind, parameters}.")
            return None
        ent = (bt.get("universe") or {}).get("entity") or (self.uni.entity if self.uni else None)
        if ent not in self.onto.entities:
            self.err(f"backtest.universe.entity `{ent}` is not an entity.")
            return None
        saved_scope, saved_uni = self.scope, self.uni
        self.uni = Universe(ent)
        self.scope = Scope(self.onto, self.uni, {}, saved_scope.params, {})
        out = {"entity": ent, "rows": self.parse("backtest.rows", bt.get("rows", "True")), "bind": {}, "parameters": {},
               "limit": int(bt.get("limit", 100))}
        self.typ(out["rows"], Mode("backtest.rows", True, False, False))
        var_names = {v.name for v in variables}
        for var, text in (bt.get("bind") or {}).items():
            if var not in var_names:
                self.err(f"backtest.bind: `{var}` is not a declared variable.")
            e = self.parse(f"backtest.bind.{var}", text)
            self.typ(e, Mode(f"backtest.bind.{var}", True, False, False))
            out["bind"][var] = e
        for p, text in (bt.get("parameters") or {}).items():
            if p not in {x.name for x in params}:
                self.err(f"backtest.parameters: `{p}` is not a declared parameter.")
            e = self.parse(f"backtest.parameters.{p}", text)
            self.typ(e, Mode(f"backtest.parameters.{p}", True, False, False))
            out["parameters"][p] = e
        self.scope, self.uni = saved_scope, saved_uni
        return out

    def grounding(self, g: dict, uni):
        if not isinstance(g, dict):
            self.err("`grounding` must be an object.")
            return
        for key in ("terms", "relations"):
            for ident in g.get(key) or []:
                if not self.onto.exists(str(ident)):
                    close = difflib.get_close_matches(str(ident), list(self.onto.by_id), n=2, cutoff=0.6)
                    self.err(f"grounding.{key}: `{ident}` is not in the ontology." +
                             (f" Did you mean {', '.join(close)}?" if close else ""))
        addressed = set()
        for key, need in (("constraints_applied", "how"), ("constraints_not_applicable", "why")):
            for item in g.get(key) or []:
                if not isinstance(item, dict) or not item.get("id") or not item.get(need):
                    self.err(f"grounding.{key}: each entry must be {{\"id\": ..., \"{need}\": ...}}.")
                    continue
                if item["id"] not in self.onto.constraints:
                    self.err(f"grounding.{key}: `{item['id']}` is not an ontology constraint.")
                addressed.add(item["id"])
        used_fields = [f for _, f in self.used.values()]
        used_entities = set(uni.aliases.values()) if uni else set()
        for c in triggered_block_constraints(self.onto, self.ctx.question, used_fields, used_entities):
            if c.id not in addressed:
                self.err(f"block constraint `{c.id}` applies (its trigger words, fields or entities appear) but is not "
                         f"addressed. Add it to grounding.constraints_applied with how the model honours it, or to "
                         f"constraints_not_applicable with why. It says: {' '.join(c.description.split())[:300]}")

    def cost(self, uni) -> Optional[str]:
        if not uni or not self.ctx.catalog or not self.ctx.snapshot:
            return None
        snap = self.ctx.snapshot.as_dict()
        total = 0
        for ent in set(uni.aliases.values()):
            table = self.onto.entities[ent].table
            if table in snap:
                total += self.ctx.catalog.row_count(table, snap[table]) or 0
        limit = self.ctx.cfg.get("governance", {}).get("max_rows_without_approval", 50_000_000)
        if total > limit and not self.ctx.principal.large_queries:
            return f"the model scans about {total:,} rows, above the {limit:,} limit for your role"
        return None


class _Stop(Exception):
    """Abort typing of one expression after its error was recorded."""


def _union(ts) -> frozenset:
    out = frozenset()
    for t in ts:
        out |= t.aliases
    return out


def validate(raw: Any, ctx: ValidationContext) -> ValidationResult:
    return _V(raw, ctx).run()
