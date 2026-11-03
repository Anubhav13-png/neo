"""The ontology object model and the loaded, indexed Ontology a question works against.

Document format (one JSON object, possibly split across several source files):
  terms          business vocabulary: id, name, kind, definition, aliases, domain, owner, status
  entities       things with rows: id, name, table (physical), key (field), grain, time_field, classification
  fields         mappings: id, entity, name (used in expressions), term, column (physical) or expression
                 (over other fields of the same entity), unit, time_semantics (stock|flow|ytd), per_period,
                 classification (public|internal|confidential|restricted), aliases, description
  relations      kind 'join' (from_entity, to_entity, on [[from_field, to_field]], cardinality) or
                 kind 'semantic' (source, target terms, description)
  constraints    id, kind, severity (block|info), target (any id), triggers, description, formula
  metrics        approved formulas: id, name, entity, expression (aggregate over entity fields), unit
  scenario_sets  curated windows of history: id, name, entity, rows (filter), description, approved_by
  evidence       id, source, query, result, timestamp
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, fields as dc_fields
from typing import Dict, List, Optional, Tuple

CLEARANCE = ["public", "internal", "confidential", "restricted"]
CARDINALITIES = ("many_to_one", "one_to_one", "one_to_many", "many_to_many")
TIME_SEMANTICS = (None, "stock", "flow", "ytd")


def _build(cls, d: dict):
    names = {f.name for f in dc_fields(cls)}
    known = {k: v for k, v in d.items() if k in names}
    obj = cls(**known)
    obj.extra = {k: v for k, v in d.items() if k not in names}
    return obj


@dataclass
class Term:
    id: str
    name: str = ""
    kind: str = "concept"
    definition: str = ""
    aliases: List[str] = field(default_factory=list)
    domain: str = ""
    owner: str = ""
    status: str = "approved"
    evidence: List[str] = field(default_factory=list)


@dataclass
class Entity:
    id: str
    name: str = ""
    table: str = ""
    key: str = ""
    grain: str = ""
    time_field: Optional[str] = None
    definition: str = ""
    aliases: List[str] = field(default_factory=list)
    domain: str = ""
    owner: str = ""
    classification: str = "internal"


@dataclass
class Field:
    id: str
    entity: str
    name: str
    term: Optional[str] = None
    column: Optional[str] = None
    expression: Optional[str] = None
    unit: Optional[str] = None
    time_semantics: Optional[str] = None
    per_period: Optional[str] = None
    classification: str = "internal"
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    dtype: Optional[str] = None            # filled from the catalog schema: number | text | date | bool


@dataclass
class Relation:
    id: str
    kind: str = "join"
    from_entity: Optional[str] = None
    to_entity: Optional[str] = None
    on: List[List[str]] = field(default_factory=list)
    cardinality: Optional[str] = None
    alias: Optional[str] = None
    source: Optional[str] = None
    target: Optional[str] = None
    description: str = ""


@dataclass
class Constraint:
    id: str
    kind: str = "data_quality"
    severity: str = "info"
    target: Optional[str] = None
    triggers: List[str] = field(default_factory=list)
    description: str = ""
    formula: Optional[str] = None


@dataclass
class Metric:
    id: str
    name: str = ""
    entity: str = ""
    expression: str = ""
    unit: Optional[str] = None
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    owner: str = ""


@dataclass
class ScenarioSet:
    id: str
    name: str = ""
    entity: str = ""
    rows: str = "True"
    description: str = ""
    approved_by: Optional[str] = None
    aliases: List[str] = field(default_factory=list)


@dataclass
class Evidence:
    id: str
    source: str = ""
    query: str = ""
    result: str = ""
    timestamp: str = ""


KINDS = {"terms": Term, "entities": Entity, "fields": Field, "relations": Relation, "constraints": Constraint,
         "metrics": Metric, "scenario_sets": ScenarioSet, "evidence": Evidence}


class Ontology:
    """One published ontology version, indexed for lookups, joins and retrieval."""

    def __init__(self, document: dict, version: str = "unversioned"):
        self.version = version
        self.document = document
        self.objects: Dict[str, List] = {k: [_build(cls, d) for d in document.get(k, [])] for k, cls in KINDS.items()}
        self.by_id: Dict[str, object] = {}
        for objs in self.objects.values():
            for o in objs:
                self.by_id[o.id] = o
        self.terms = {t.id: t for t in self.objects["terms"]}
        self.entities = {e.id: e for e in self.objects["entities"]}
        self.fields: Dict[str, Dict[str, Field]] = defaultdict(dict)
        for f in self.objects["fields"]:
            self.fields[f.entity][f.name] = f
        self.relations = {r.id: r for r in self.objects["relations"]}
        self.constraints = {c.id: c for c in self.objects["constraints"]}
        self.metrics = {m.id: m for m in self.objects["metrics"]}
        self.scenario_sets = {s.id: s for s in self.objects["scenario_sets"]}
        self.joins: Dict[str, List[Tuple[Relation, bool]]] = defaultdict(list)   # entity -> (relation, reversed)
        for r in self.objects["relations"]:
            if r.kind == "join":
                self.joins[r.from_entity].append((r, False))
                if r.to_entity != r.from_entity:
                    self.joins[r.to_entity].append((r, True))

    # ------------------------------------------------------------------ lookups
    def field(self, entity: str, name: str) -> Optional[Field]:
        return self.fields.get(entity, {}).get(name)

    def fields_of(self, entity: str) -> List[Field]:
        return list(self.fields.get(entity, {}).values())

    def exists(self, ident: str) -> bool:
        return ident in self.by_id

    @staticmethod
    def reverse_cardinality(card: str) -> str:
        return {"many_to_one": "one_to_many", "one_to_many": "many_to_one"}.get(card, card)

    def join_direction(self, rel: Relation, from_entity: str, reverse: bool = False):
        """(target entity, [(from_field, to_field)], cardinality seen from `from_entity`) or None."""
        if rel.kind != "join":
            return None
        forward_ok = rel.from_entity == from_entity
        backward_ok = rel.to_entity == from_entity
        if forward_ok and not (reverse and backward_ok):
            return rel.to_entity, [tuple(p) for p in rel.on], rel.cardinality
        if backward_ok:
            return rel.from_entity, [(b, a) for a, b in rel.on], self.reverse_cardinality(rel.cardinality)
        return None

    def join_path(self, start: str, goal: str, max_hops: int = 4) -> Optional[List[Tuple[Relation, bool]]]:
        """Shortest chain of join relations from `start` to `goal` (breadth-first)."""
        if start == goal:
            return []
        frontier, seen = [(start, [])], {start}
        for _ in range(max_hops):
            nxt = []
            for ent, path in frontier:
                for rel, rev in self.joins.get(ent, []):
                    other = rel.from_entity if rev else rel.to_entity
                    if other in seen:
                        continue
                    if other == goal:
                        return path + [(rel, rev)]
                    seen.add(other)
                    nxt.append((other, path + [(rel, rev)]))
            frontier = nxt
        return None

    def targets_of_field(self, f: Field) -> set:
        """Ids a constraint may target to cover this field: the field, its term, its entity."""
        return {f.id, f.entity} | ({f.term} if f.term else set())

    def domain_of(self, ident: str) -> str:
        o = self.by_id.get(ident)
        if isinstance(o, Field):
            t = self.terms.get(o.term) if o.term else None
            return (t.domain if t else "") or getattr(self.entities.get(o.entity), "domain", "")
        return getattr(o, "domain", "") or ""
