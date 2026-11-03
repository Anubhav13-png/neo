"""The exact SQL compiler and the independent interpreter agree on every row, for every kind of expression.

These are two separate implementations of the model language (integer numerator/denominator SQL inside the lake,
and Python fractions over raw columns), so agreement on every row is strong evidence that neither has a bug.
"""
from fractions import Fraction

import pytest

from neo.compiler.sql import Compiler
from neo.execution.interpreter import Interpreter
from neo.semantic.validator import ValidationContext, validate

BANK = [
    "NETINCQ / ASSET", "NETINCQ / 3", "NETINCQ / -7", "NIMQ / 4 - NONIXQ * 0.25", "NETINCQ / EQ",
    "NETINCQ // 1000", "NETINCQ % 997", "-NETINCQ // 1000", "-NETINCQ % 997",
    "ASSET if source == 'industry' else -ASSET", "min(NIMQ, NONIXQ)", "max(NIMQ, NONIXQ)", "abs(NETINCQ - ITAXQ)",
    "source in ['industry', 'bank:628']", "not (source in ['industry'])", "contains(source, 'company')",
    "startswith(lower(source), 'b')", "year_of(quarter)", "quarter >= '2008-09-30'",
    "NETINCQ > 0 and ITAXQ < 1000000 or source == 'industry'", "implies(NETINCQ < 0, ITAXQ <= 0)",
    "coalesce(IGLSECQ, 0)", "NETINCQ / (qtr - 2)", "NETINCQ / (qtr - 2) > 0", "ite(NETINCQ > 0, NIMQ, NONIIQ)",
    "1 / (BANKS - 1)", "pretax_income", "qtr * 3 - 2",
]
DEAL = [
    "target_assets_pre_deal_000 / 1e6", "target_assets_pre_deal_000 > acquirer_assets_pre_deal_000",
    "target_to_acquirer_asset_ratio * 100", "coalesce(failure_cost_to_fdic_000, 0)", "is_null(failure_date)",
    "target_lat > 40", "year // 10 * 10", "same_state", "branches_transferred / 2", "is_assisted_failed",
    "not_null(target_assets_pre_deal_000) and target_assets_pre_deal_000 / 1e6 >= 1.5",
    "buyer.target_state == target_state", "buyer.generation",
]


def compare(neo, entity, expr, joins=()):
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    eng = neo.engine.bind(snap)
    raw = {"form": "value", "interpretation": "per-row compilation check", "universe": {"entity": entity,
                                                                                      "joins": list(joins)},
           "define": {"x": expr}, "target": {"expr": "count()"},
           "grounding": {"constraints_not_applicable": [{"id": c.id, "why": "compilation test"}
                                                        for c in onto.constraints.values() if c.severity == "block"]}}
    res = validate(raw, ValidationContext(onto, "check", neo.principal("admin"), neo.rules, neo.catalog, snap, neo.cfg))
    assert not res.errors, res.errors
    m = res.model
    c = Compiler(onto, m, eng, {}, double_scale=10)
    r = c.expr(m.defines["x"])
    kf = onto.field(entity, onto.entities[entity].key)
    cols = [r.sql, r.den] if r.kind == "num" else [r.sql]
    rows = eng.execute(f"SELECT {c.field_r('', kf).sql}, {', '.join(cols)} FROM {c.from_clause()} "
                       f"WHERE {c.where_clause()}")[1]
    via_sql = {}
    for row in rows:
        if r.kind == "num":
            via_sql[row[0]] = None if row[1] is None or row[2] is None else Fraction(int(row[1]), int(row[2]))
        else:
            via_sql[row[0]] = row[1]
    it = Interpreter(onto, m, eng, {}, 10, 10 ** 7)
    via_python = {}
    for row in it.eligible():
        k = it.field_value(row, "", kf)
        via_python[int(k) if isinstance(k, Fraction) else k] = it.row_value(m.defines["x"], row)
    assert len(via_sql) == len(via_python)
    bad = [(k, via_sql[k], via_python.get(k)) for k in via_sql if via_sql[k] != via_python.get(k)]
    assert not bad, f"{expr}: {len(bad)} rows differ, e.g. {bad[:3]}"
    return len(via_sql)


@pytest.mark.parametrize("expr", BANK)
def test_bank_history_expressions(neo, expr):
    assert compare(neo, "bank_quarter", expr) == 510


@pytest.mark.parametrize("expr", DEAL)
def test_deal_expressions(neo, expr):
    assert compare(neo, "deal", expr, [{"relation": "rel_deal_buyer", "as": "buyer"}]) == 955
