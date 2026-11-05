"""The Ontology Service: loads published versions, attaches the catalog schema, and retrieves the part of
the ontology a question needs (the per-question briefing), instead of putting everything in the prompt.

retrieve():
  1. link question words to terms / entities / fields / metrics / scenario sets
  2. choose the entities in scope, find join paths between them, add 1-hop neighbours
  3. collect their fields the user may see (others are hidden and counted), constraints, metrics,
     scenario sets, rules, and column profiles (exact enum values, ranges)
  4. if nothing can be linked, ask for clarification instead of letting the model guess
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..lake.catalog import Catalog, Snapshot
from ..lake.profiles import profile_table, render_profile
from .linker import Link, Linker
from .model import CLEARANCE, Constraint, Field, Ontology
from .store import OntologyStore


def dtype_of(sql_type: str) -> str:
    t = sql_type.upper()
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "text"
    if t.startswith(("DATE", "TIMESTAMP")):
        return "date"
    if t.startswith("BOOL"):
        return "bool"
    return "number"


def attach_schema(onto: Ontology, catalog: Catalog, snapshot: Snapshot):
    """Give every column-backed field its dtype and physical SQL type from the pinned table version."""
    snap = snapshot.as_dict()
    for ent_id, fields in onto.fields.items():
        ent = onto.entities.get(ent_id)
        if not ent or ent.table not in snap:
            continue
        schema = dict(catalog.schema(ent.table, snap[ent.table]))
        for f in fields.values():
            if f.column:
                f.physical_type = schema.get(f.column)
                if f.physical_type:
                    f.dtype = dtype_of(f.physical_type)
            elif f.dtype is None:
                f.dtype = "number"


@dataclass
class Retrieval:
    question: str
    links: List[Link]
    entities: List[str]
    neighbours: List[str]
    join_paths: List[dict]
    fields: Dict[str, List[Field]]
    hidden_fields: int
    constraints: List[Constraint]
    metrics: list
    scenario_sets: list
    terms: list
    rules: list
    profiles: Dict[str, Dict[str, str]]
    needs_clarification: bool = False
    clarification: str = ""
    unlinked_words: List[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"links": [{"id": l.id, "kind": l.kind, "score": l.score, "matched": l.matched} for l in self.links],
                "entities": self.entities, "neighbours": self.neighbours, "join_paths": self.join_paths,
                "constraints": [c.id for c in self.constraints], "metrics": [m.id for m in self.metrics],
                "scenario_sets": [s.id for s in self.scenario_sets], "rules": [r["id"] for r in self.rules],
                "hidden_fields": self.hidden_fields, "needs_clarification": self.needs_clarification,
                "unlinked_words": self.unlinked_words}


def word_triggered(c: Constraint, question: str) -> bool:
    q = question.lower()
    return any(k and re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", q) for k in c.triggers)


WORD_ONLY_KINDS = ("legal_rule", "methodology")


def attached_targets(onto: Ontology) -> Set[str]:
    """Ids that belong to some entity: entities, their fields, and the terms those fields map to."""
    if not hasattr(onto, "_attached"):
        out = set(onto.entities)
        for fs in onto.fields.values():
            for f in fs.values():
                out |= onto.targets_of_field(f)
        onto._attached = out
    return onto._attached


def scope_targets(onto: Ontology, entities: Set[str], fields: List[Field]) -> Set[str]:
    targets = set(entities)
    for ent in entities:
        for f in onto.fields_of(ent):
            targets |= onto.targets_of_field(f)
    for f in fields:
        targets |= onto.targets_of_field(f)
    return targets


def triggered_block_constraints(onto: Ontology, question: str, used_fields: List[Field],
                                used_entities: Set[str], severity: str = "block") -> List[Constraint]:
    """`block` constraints a model must address.

    By wording: the question contains a trigger word, and the constraint is about the model's entities (or about
    no entity at all, like a legal rule on a concept). A rule about one entity does not fire on a model over another
    entity just because both questions use the same word.
    By use: the model reads a field (or entity) the constraint targets. Legal rules and methodology rules are
    triggered only by wording, since they are about what is asked, not which columns are touched.
    """
    in_scope = scope_targets(onto, used_entities, used_fields)
    used = set(used_entities)
    for f in used_fields:
        used |= onto.targets_of_field(f)
    attached = attached_targets(onto)
    out = []
    for c in onto.constraints.values():
        if c.severity != severity:
            continue
        relevant = c.target not in attached or c.target in in_scope
        by_word = relevant and word_triggered(c, question)
        by_use = c.kind not in WORD_ONLY_KINDS and c.target in used
        if by_word or by_use:
            out.append(c)
    return out


def owed_caveats(onto: Ontology, question: str, used_fields: List[Field], used_entities: Set[str]) -> List[dict]:
    """`info` constraints that apply to a question the same way block ones do: the answer must mention them."""
    return [{"id": c.id, "description": " ".join(c.description.split())}
            for c in triggered_block_constraints(onto, question, used_fields, used_entities, severity="info")]


class OntologyService:
    def __init__(self, store: OntologyStore, catalog: Catalog):
        self.store = store
        self.catalog = catalog
        self._cache: Dict[tuple, Ontology] = {}
        self._linkers: Dict[str, Linker] = {}

    def load(self, version: Optional[str], snapshot: Snapshot) -> Ontology:
        version = version or self.store.current_version()
        key = (version, snapshot.id)
        if key not in self._cache:
            onto = self.store.load(version)
            attach_schema(onto, self.catalog, snapshot)
            self._cache[key] = onto
        return self._cache[key]

    def linker(self, onto: Ontology) -> Linker:
        if onto.version not in self._linkers:
            self._linkers[onto.version] = Linker(onto)
        return self._linkers[onto.version]

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, onto: Ontology, question: str, clearance: str, engine, snapshot: Snapshot,
                 rules: list, cfg: dict, restricted: Optional[Set[str]] = None) -> Retrieval:
        """restricted: entities under a row policy for this user (their profiles are rendered without counts)."""
        rc = cfg.get("retrieval", {})
        linker = self.linker(onto)
        links = linker.link(question, limit=rc.get("max_linked", 25))
        score: Dict[str, float] = defaultdict(float)
        for l in links:
            obj = onto.by_id.get(l.id)
            if l.kind == "entity":
                score[l.id] += l.score * 1.5
            elif l.kind in ("field", "metric", "scenario_set"):
                score[obj.entity] += l.score
            elif l.kind == "term":
                for ent, fs in onto.fields.items():
                    if any(f.term == l.id for f in fs.values()):
                        score[ent] += l.score * 0.8
        ranked = sorted(score, key=lambda e: -score[e])
        top = score[ranked[0]] if ranked else 0
        entities = [e for e in ranked if score[e] >= 0.25 * top][:4]
        if not entities:
            names = ", ".join(f"{e.name or e.id}" for e in onto.entities.values())
            return Retrieval(question, links, [], [], [], {}, 0, [], [], [], [], [], {}, True,
                             f"I could not connect this question to the data. Which of these is it about: {names}?",
                             linker.unlinked_words(question))
        join_paths, neighbours = [], []
        for other in entities[1:]:
            path = onto.join_path(entities[0], other)
            join_paths.append({"from": entities[0], "to": other,
                               "relations": [r.id for r, _ in path] if path is not None else None})
            for rel, _ in path or []:
                for ent in (rel.from_entity, rel.to_entity):
                    if ent not in entities and ent not in neighbours:
                        neighbours.append(ent)
        if rc.get("hops", 1) >= 1:
            for ent in list(entities):
                for rel, rev in onto.joins.get(ent, []):
                    other = rel.from_entity if rev else rel.to_entity
                    if other not in entities and other not in neighbours:
                        neighbours.append(other)
        in_scope = entities + neighbours
        level = CLEARANCE.index(clearance)
        fields, hidden = {}, 0
        for ent in in_scope:
            visible = []
            for f in onto.fields_of(ent):
                if CLEARANCE.index(f.classification) <= level:
                    visible.append(f)
                else:
                    hidden += 1
            fields[ent] = visible[: rc.get("max_fields_per_entity", 120)]
        targets = set(in_scope)
        linked_terms = {l.id for l in links if l.kind == "term"}
        for fs in fields.values():
            for f in fs:
                targets |= onto.targets_of_field(f)
        attached = attached_targets(onto)
        constraints = [c for c in onto.constraints.values()
                       if c.target in targets or (c.target not in attached and
                                                  (c.target in linked_terms or word_triggered(c, question)))]
        metrics = [m for m in onto.metrics.values() if m.entity in in_scope]
        scenario_sets = [s for s in onto.scenario_sets.values() if s.entity in in_scope]
        term_ids = linked_terms | {f.term for fs in fields.values() for f in fs if f.term}
        terms = [onto.terms[t] for t in sorted(term_ids) if t in onto.terms]
        q_rules = [r for r in rules if r.get("status") == "approved" and
                   (set(r.get("terms") or []) & term_ids or any(
                       re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", question.lower())
                       for k in r.get("triggers") or []))]
        profiles: Dict[str, Dict[str, str]] = {}
        snap = snapshot.as_dict()
        for ent in in_scope:
            e = onto.entities[ent]
            if e.table in snap and hasattr(self.catalog, "profile_path"):
                prof = profile_table(engine, self.catalog, e.table, snap[e.table])
                counts = ent not in (restricted or set())
                profiles[ent] = {f.name: render_profile(prof[f.column], counts) for f in fields[ent]
                                 if f.column and f.column in prof}
        return Retrieval(question, links, entities, neighbours, join_paths, fields, hidden, constraints, metrics,
                         scenario_sets, terms, q_rules, profiles, unlinked_words=linker.unlinked_words(question))

    # ------------------------------------------------------------------ briefing
    @staticmethod
    def render(onto: Ontology, r: Retrieval) -> str:
        L = [f"# Ontology (version {onto.version}; only the part relevant to this question)"]
        L.append("\n## Entities in scope (the universe of a model is one of these, joined to others)")
        for ent in r.entities + r.neighbours:
            e = onto.entities[ent]
            tag = "" if ent in r.entities else " (related)"
            time = f"; time field {e.time_field}" if e.time_field else ""
            L.append(f"- entity `{e.id}`{tag}: {e.name}. One row per {e.grain}; key `{e.key}`{time}. {e.definition}")
            for f in r.fields.get(ent, []):
                bits = [f.dtype or "number"]
                if f.unit:
                    bits.append(f.unit)
                if f.time_semantics:
                    bits.append({"stock": "level at a point in time", "flow": "amount over one period",
                                 "ytd": f"YEAR-TO-DATE (use `{f.per_period}` for one period)"}[f.time_semantics])
                if f.classification != "public":
                    bits.append(f.classification)
                prof = r.profiles.get(ent, {}).get(f.name)
                src = f"= {f.expression}" if f.expression else ""
                term = f" [{f.term}]" if f.term else ""
                L.append(f"    {f.name}{term}: {', '.join(bits)}{(' ' + src) if src else ''}"
                         f"{(' — ' + f.description) if f.description else ''}{(' | ' + prof) if prof else ''}")
        joins = [rel for rel in onto.relations.values() if rel.kind == "join" and
                 rel.from_entity in r.entities + r.neighbours and rel.to_entity in r.entities + r.neighbours]
        if joins:
            L.append("\n## Joins (use them by id in `universe.joins`; cardinality decides whether sums double count)")
            for rel in joins:
                on = " and ".join(f"{rel.from_entity}.{a} = {rel.to_entity}.{b}" for a, b in rel.on)
                L.append(f"- {rel.id}: {rel.from_entity} -> {rel.to_entity} on {on} ({rel.cardinality}; reversed it is "
                         f"{onto.reverse_cardinality(rel.cardinality)}). Suggested alias `{rel.alias or rel.to_entity}`. "
                         f"{rel.description}")
        for jp in r.join_paths:
            if jp["relations"] is None:
                L.append(f"- NOTE: no join path connects `{jp['from']}` and `{jp['to']}`.")
        if r.metrics:
            L.append("\n## Approved metrics (prefer these formulas)")
            for m in r.metrics:
                L.append(f"- {m.id} ({m.entity}): {m.name} = `{m.expression}` [{m.unit or ''}] {m.description}")
        if r.scenario_sets:
            L.append("\n## Scenario sets (curated windows of history; `universe.scenario_set`)")
            for s in r.scenario_sets:
                L.append(f"- {s.id} ({s.entity}): {s.name}: rows `{s.rows}`. {s.description}")
        if r.constraints:
            L.append("\n## Constraints (`block` ones must be addressed in grounding when they apply)")
            for c in r.constraints:
                trig = f" | triggers: {', '.join(c.triggers[:8])}" if c.triggers else ""
                formula = f" | formula: {c.formula}" if c.formula else ""
                L.append(f"- {c.id} [{c.severity}, {c.kind}] on {c.target}: {' '.join(c.description.split())}"
                         f"{formula}{trig}")
        if r.terms:
            L.append("\n## Terms")
            for t in r.terms:
                L.append(f"- {t.id}: {t.name}. {' '.join(t.definition.split())[:300]}")
        rel_sem = [rel for rel in onto.relations.values() if rel.kind == "semantic" and
                   (rel.source in {t.id for t in r.terms} or rel.target in {t.id for t in r.terms})]
        if rel_sem:
            L.append("\n## Semantic relations")
            for rel in rel_sem[:30]:
                L.append(f"- {rel.id}: {rel.source} -> {rel.target}. {rel.description}")
        if r.rules:
            L.append("\n## Approved rules (cite as source: rule:<id>; parameters via rule_param)")
            for rule in r.rules:
                params = ", ".join(f"rule_param('{rule['id']}', '{k}') = {v.get('value')}"
                                   for k, v in (rule.get("parameters") or {}).items())
                L.append(f"- {rule['id']} v{rule.get('version', 1)}: {rule.get('name', '')}. formula: "
                         f"{rule.get('formula', '')}. {params}. text: {' '.join(str(rule.get('text', '')).split())[:600]}")
        if r.hidden_fields:
            L.append(f"\n({r.hidden_fields} fields of these entities are hidden by your access level; do not use them.)")
        return "\n".join(L)
