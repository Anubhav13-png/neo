"""The validated semantic model and the name scope shared by the validator, compiler and interpreter.

The LLM writes JSON in ontology vocabulary (see prompts/manual.md). After validation it becomes a
SemanticModel: every name resolved to an ontology field, a define, a parameter or a variable, every join
resolved to a relation with its cardinality, and the user's row policies attached per alias.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..dsl import Expr
from ..ontology.model import Field, Ontology
from .units import Unit

FORMS = ("value", "table", "extremum", "set", "check", "feasibility", "optimisation", "not_expressible")
SORTS = ("Real", "Int", "Bool")


@dataclass
class JoinSpec:
    alias: str
    relation: str
    entity: str
    from_alias: str                      # "" = the root
    pairs: List[Tuple[str, str]]         # (field on from_alias, field on alias)
    cardinality: str                     # as seen from from_alias


@dataclass
class Universe:
    entity: str
    joins: List[JoinSpec] = field(default_factory=list)
    scenario_set: Optional[str] = None
    grain: Optional[str] = None          # alias whose rows the universe has, when a join fans out
    fanned: Set[str] = field(default_factory=set)

    @property
    def aliases(self) -> Dict[str, str]:
        d = {"": self.entity}
        for j in self.joins:
            d[j.alias] = j.entity
        return d


@dataclass
class Param:
    name: str
    expr: Expr
    unit: Optional[Unit] = None
    has_agg: bool = False
    stated: bool = False                 # a number the question states, not taken from data


@dataclass
class Var:
    name: str
    sort: str
    domain: Optional[Expr] = None
    unit: Optional[Unit] = None


@dataclass
class Named:
    name: str
    expr: Expr
    source: str = ""


@dataclass
class SemanticModel:
    raw: dict
    form: str
    interpretation: str
    universe: Optional[Universe]
    rows: Optional[Expr]
    policies: Dict[str, Expr]            # alias -> row policy expression (fields of that alias's entity)
    defines: Dict[str, Expr]
    group_by: Optional[Expr]
    params: List[Param]
    variables: List[Var]
    constraints: List[Named]
    assumptions: List[Named]
    target: Dict[str, Any]
    sensitivity: List[dict]
    backtest: Optional[dict]
    grounding: dict
    used_fields: List[Tuple[str, Field]] = field(default_factory=list)
    notes: Dict[str, Any] = field(default_factory=dict)
    needs_approval: Optional[str] = None

    def param(self, name: str) -> Optional[Param]:
        return next((p for p in self.params if p.name == name), None)


class Scope:
    """Resolves a name in a model: root field, alias.field, define, parameter or variable."""

    def __init__(self, onto: Ontology, universe: Universe, defines: Dict[str, Expr], params: Dict[str, Any],
                 variables: Dict[str, Any]):
        self.onto = onto
        self.universe = universe
        self.defines = defines
        self.params = params
        self.variables = variables
        self.aliases = universe.aliases if universe else {}

    def field(self, name: str) -> Optional[Tuple[str, Field]]:
        if "." in name:
            alias, fname = name.split(".", 1)
            if alias not in self.aliases or alias == "":
                return None
            f = self.onto.field(self.aliases[alias], fname)
            return (alias, f) if f else None
        if not self.universe:
            return None
        f = self.onto.field(self.universe.entity, name)
        return ("", f) if f else None

    def kind(self, name: str) -> Optional[str]:
        if name in self.defines:
            return "define"
        if name in self.params:
            return "param"
        if name in self.variables:
            return "var"
        if self.field(name):
            return "field"
        return None
