"""Publish-time checks: an ontology version is only as good as its mappings, so every reference, physical
column, expression, unit and join key is checked against the catalog before a version can be published."""
from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from ..dsl import AGGREGATES, ExprError, SCALARS, parse, refs
from ..semantic.units import valid_unit
from .model import CARDINALITIES, CLEARANCE, KINDS, TIME_SEMANTICS, Ontology


def lint(document: dict, catalog=None, snapshot=None) -> Tuple[List[str], List[str]]:
    errors, warnings = [], []
    ids = Counter(d.get("id") for k in KINDS for d in document.get(k, []))
    for ident, n in ids.items():
        if not ident:
            errors.append("an object has no id")
        elif n > 1:
            errors.append(f"id `{ident}` is used {n} times")
    onto = Ontology(document, "candidate")
    snap = snapshot.as_dict() if snapshot else {}
    schemas = {}
    for e in onto.entities.values():
        if not e.table:
            errors.append(f"entity `{e.id}` has no table")
            continue
        if e.classification not in CLEARANCE:
            errors.append(f"entity `{e.id}`: classification must be one of {CLEARANCE}")
        if catalog is not None:
            if e.table not in snap:
                errors.append(f"entity `{e.id}`: table `{e.table}` is not in the lake")
            else:
                schemas[e.id] = dict(catalog.schema(e.table, snap[e.table]))
        if e.key and not onto.field(e.id, e.key):
            errors.append(f"entity `{e.id}`: key `{e.key}` is not one of its fields")
        if e.time_field and not onto.field(e.id, e.time_field):
            errors.append(f"entity `{e.id}`: time_field `{e.time_field}` is not one of its fields")
    for f in onto.objects["fields"]:
        where = f"field `{f.entity}.{f.name}`"
        if f.entity not in onto.entities:
            errors.append(f"{where}: entity `{f.entity}` does not exist")
            continue
        if not f.name.isidentifier() or f.name.startswith("_"):
            errors.append(f"{where}: name must be an identifier")
        if f.term and f.term not in onto.terms:
            errors.append(f"{where}: term `{f.term}` does not exist")
        if bool(f.column) == bool(f.expression):
            errors.append(f"{where}: needs exactly one of `column` or `expression`")
        if f.column and f.entity in schemas and f.column not in schemas[f.entity]:
            errors.append(f"{where}: column `{f.column}` is not in table `{onto.entities[f.entity].table}`")
        if f.expression:
            try:
                names, calls = refs(parse(f.expression))
                for nm in names:
                    if not onto.field(f.entity, nm):
                        errors.append(f"{where}: expression uses `{nm}`, not a field of `{f.entity}`")
                if calls & set(AGGREGATES):
                    errors.append(f"{where}: a field expression is per row; it cannot use aggregates")
                if calls - set(SCALARS) - set(AGGREGATES):
                    errors.append(f"{where}: unknown function(s) {sorted(calls - set(SCALARS))}")
            except ExprError as e:
                errors.append(f"{where}: {e}")
            if not f.dtype:
                warnings.append(f"{where}: derived field without `dtype`; assuming number")
        if not valid_unit(f.unit):
            errors.append(f"{where}: unknown unit `{f.unit}`")
        if f.time_semantics not in TIME_SEMANTICS:
            errors.append(f"{where}: time_semantics must be stock, flow or ytd")
        if f.time_semantics == "ytd" and not (f.per_period and onto.field(f.entity, f.per_period)):
            errors.append(f"{where}: a year-to-date field needs `per_period` naming its one-period field")
        if f.classification not in CLEARANCE:
            errors.append(f"{where}: classification must be one of {CLEARANCE}")
    for r in onto.objects["relations"]:
        if r.kind == "join":
            for ent in (r.from_entity, r.to_entity):
                if ent not in onto.entities:
                    errors.append(f"relation `{r.id}`: entity `{ent}` does not exist")
            if r.cardinality not in CARDINALITIES:
                errors.append(f"relation `{r.id}`: cardinality must be one of {CARDINALITIES}")
            if not r.on:
                errors.append(f"relation `{r.id}`: join needs `on` [[from_field, to_field]]")
            for pair in r.on:
                if len(pair) != 2 or not onto.field(r.from_entity, pair[0]) or not onto.field(r.to_entity, pair[1]):
                    errors.append(f"relation `{r.id}`: join keys {pair} are not fields of {r.from_entity} / {r.to_entity}")
        elif r.kind == "semantic":
            for t in (r.source, r.target):
                if t and t not in onto.by_id:
                    warnings.append(f"relation `{r.id}`: `{t}` does not exist")
        else:
            errors.append(f"relation `{r.id}`: kind must be join or semantic")
    for c in onto.objects["constraints"]:
        if c.severity not in ("block", "info"):
            errors.append(f"constraint `{c.id}`: severity must be block or info")
        if c.target and c.target not in onto.by_id:
            errors.append(f"constraint `{c.id}`: target `{c.target}` does not exist")
    for m in onto.objects["metrics"]:
        _check_entity_expr(onto, f"metric `{m.id}`", m.entity, m.expression, errors, allow_agg=True)
    for s in onto.objects["scenario_sets"]:
        _check_entity_expr(onto, f"scenario set `{s.id}`", s.entity, s.rows, errors, allow_agg=False)
    aliases = Counter()
    for t in onto.terms.values():
        for a in [t.name] + t.aliases:
            aliases[(a or "").lower()] += 1
    for a, n in aliases.items():
        if a and n > 1:
            warnings.append(f"alias `{a}` names {n} different terms; linking may be ambiguous")
    return errors, warnings


def _check_entity_expr(onto, where, entity, text, errors, allow_agg):
    if entity not in onto.entities:
        errors.append(f"{where}: entity `{entity}` does not exist")
        return
    try:
        names, calls = refs(parse(text))
    except ExprError as e:
        errors.append(f"{where}: {e}")
        return
    for nm in names:
        if "." not in nm and not onto.field(entity, nm):
            errors.append(f"{where}: `{nm}` is not a field of `{entity}`")
    if not allow_agg and calls & set(AGGREGATES):
        errors.append(f"{where}: aggregates are not allowed here")
