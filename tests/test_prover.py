"""Every form is proved, and every answer equals an independent computation (hand-written SQL or plain Python)."""
from decimal import Decimal
from fractions import Fraction

import pytest

from neo.proof.prover import ProofError, Prover
from neo.semantic.validator import ValidationContext, validate

from conftest import direct

BLOCKS_DEAL = {"constraints_not_applicable": [{"id": i, "why": "test"} for i in (
    "con_unit_thousands", "con_predeal_coverage", "con_failure_detail_coverage", "con_jpm_identity_by_cert",
    "con_acquirer_not_always_jpm", "con_price_unreliable", "con_fdic_cost_blank_vs_zero", "con_unit_fdic_cost")]}


def prove(neo, raw, user="admin", question="q"):
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    res = validate(raw, ValidationContext(onto, question, neo.principal(user), neo.rules, neo.catalog, snap, neo.cfg))
    assert not res.errors, res.errors
    return Prover(onto, res.model, neo.engine.bind(snap), neo.cfg, neo.rules).run()


def test_value_columns(neo):
    p = prove(neo, {"form": "value", "interpretation": "card exposure, expected loss and default rate",
                    "universe": {"entity": "loan"}, "rows": "product == 'Card'",
                    "target": {"columns": {"exp": "sum(balance_usd)", "el": "sum(expected_loss_usd)",
                                           "dr": "share(status == 'default')", "avg_pd": "avg(pd)"}}})
    (exp, n, nd), = direct(neo, "SELECT SUM(balance_usd), COUNT(*), COUNT(*) FILTER (WHERE status='default') "
                                "FROM {loans} WHERE product = 'Card'")
    rows = direct(neo, "SELECT balance_usd, pd, lgd FROM {loans} WHERE product = 'Card'")
    el = sum(Fraction(b) * Fraction(pd) * Fraction(lgd) for b, pd, lgd in rows)
    avg_pd = sum(Fraction(pd) for _, pd, _ in rows) / len(rows)
    assert p["status"] == "proved" and p["self_check"]["matches"] and p["exactness"]["exact"]
    assert p["answer"]["exp"] == exp and abs(p["answer"]["el"] - float(el)) < 1e-6
    assert p["exact_values"]["el"] == f"{el.numerator}/{el.denominator}"
    assert Fraction(p["exact_values"]["dr"]) == Fraction(nd, n)
    assert Fraction(p["exact_values"]["avg_pd"]) == avg_pd


def test_table_with_join(neo):
    p = prove(neo, {"form": "table", "interpretation": "exposure by customer segment",
                    "universe": {"entity": "loan", "joins": [{"relation": "rel_loan_customer", "as": "cust"}]},
                    "group_by": "cust.segment", "target": {"columns": {"exp": "sum(balance_usd)", "n": "count()"}}})
    want = dict((s, (e, n)) for s, e, n in direct(
        neo, "SELECT c.segment, SUM(l.balance_usd), COUNT(*) FROM {loans} l JOIN {customers} c USING (customer_id) "
             "GROUP BY 1"))
    assert p["status"] == "proved" and p["self_check"]["matches"]
    assert {r["group"]: (r["exp"], r["n"]) for r in p["answer"]} == want


def test_extremum_rows_and_groups(neo):
    p = prove(neo, {"form": "extremum", "interpretation": "three largest deals by pre-deal assets in $B",
                    "universe": {"entity": "deal"}, "target": {"expr": "target_assets_pre_deal_000 / 1e6",
                                                               "sense": "max", "top": 3}, "grounding": BLOCKS_DEAL})
    want = [r[0] for r in direct(neo, "SELECT target_cert FROM {deals} WHERE target_assets_pre_deal_000 IS NOT NULL "
                                      "ORDER BY target_assets_pre_deal_000 DESC LIMIT 3")]
    assert p["status"] == "proved" and [r["target_cert"] for r in p["answer"]] == want
    assert all(r["rows_beating_it"] == i for i, r in enumerate(p["answer"]))
    g = prove(neo, {"form": "extremum", "interpretation": "state with the most acquired banks",
                    "universe": {"entity": "deal"}, "group_by": "target_state",
                    "target": {"expr": "count()", "sense": "max"}, "grounding": BLOCKS_DEAL})
    (st, n), = direct(neo, "SELECT target_state, COUNT(*) FROM {deals} GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    assert g["answer"]["group"] == st and g["answer"]["value"] == n and g["self_check"]["matches"]


def test_set_and_check(neo):
    s = prove(neo, {"form": "set", "interpretation": "defaulted mortgages over one million",
                    "universe": {"entity": "loan"}, "target": {"select": ["balance_usd"], "limit": 5,
                    "where": "product == 'Mortgage' and status == 'default' and balance_usd > 1000000"}})
    (n,), = direct(neo, "SELECT COUNT(*) FROM {loans} WHERE product='Mortgage' AND status='default' AND balance_usd > 1000000")
    assert s["answer"]["count"] == n and s["self_check"]["matches"]
    c = prove(neo, {"form": "check", "interpretation": "every deal of generation 1 has acquirer cert 628",
                    "universe": {"entity": "deal"},
                    "target": {"scope": "rows", "claim": "(generation == 1) == (acquirer_cert == 628)"},
                    "grounding": BLOCKS_DEAL})
    assert c["status"] == "proved" and c["self_check"]["matches"]
    r = prove(neo, {"form": "check", "interpretation": "every loan has PD below 10%", "universe": {"entity": "loan"},
                    "target": {"scope": "rows", "claim": "pd < 0.1"}})
    (bad,), = direct(neo, "SELECT COUNT(*) FROM {loans} WHERE pd >= 0.1")
    assert r["status"] == "refuted" and str(bad) in r["answer"] and r["evidence"]["counterexamples"]


def margins(neo):
    return sorted(Fraction(rate, 10000) - Fraction(Decimal(pd)) * Fraction(Decimal(lgd))
                  for rate, pd, lgd in direct(neo, "SELECT rate_bp, pd, lgd FROM {loans}"))


def test_break_even_by_refinement_on_every_loan(neo):
    """Largest uniform rate cut that keeps every loan's rate above its expected loss rate: 20,000 rows, more than
    the 5,000 the solver takes directly, so it is solved by counterexample-guided refinement."""
    neo.cfg["proof"]["cegar_seed_rows"] = 50
    raw = {"form": "optimisation", "interpretation": "largest uniform rate cut keeping rate >= pd x lgd on every loan",
           "universe": {"entity": "loan"},
           "variables": [{"name": "cut", "sort": "Real", "domain": "cut >= -1", "unit": "ratio"}],
           "constraints": [{"name": "every_loan", "expr": "all_rows(rate - cut >= pd * lgd)"}],
           "target": {"objective": "cut", "sense": "max"}}
    p = prove(neo, raw)
    assert p["status"] == "optimal" and p["solver"]["mode"] == "cegar" and p["solver"]["rounds"] >= 2
    assert Fraction(p["exact_values"]["objective"]) == margins(neo)[0]
    raw["constraints"] = [{"name": "at_most_10_bad", "expr": "count(rate - cut < pd * lgd) <= 10"}]
    q = prove(neo, raw)
    assert Fraction(q["exact_values"]["objective"]) == margins(neo)[10]


def test_break_even_direct_matches_refinement(neo):
    raw = {"form": "optimisation", "interpretation": "largest uniform rate cut on card loans",
           "universe": {"entity": "loan"}, "rows": "product == 'Card' and booking_region == 'EMEA'",
           "variables": [{"name": "cut", "sort": "Real", "unit": "ratio"}],
           "constraints": [{"name": "every_loan", "expr": "all_rows(rate - cut >= pd * lgd)"}],
           "target": {"objective": "cut", "sense": "max"}}
    full = prove(neo, raw)
    neo.cfg["proof"]["symbolic_max_rows"] = 10
    neo.cfg["proof"]["cegar_seed_rows"] = 5
    ref = prove(neo, raw)
    assert full["solver"]["mode"] == "full" and ref["solver"]["mode"] == "cegar"
    assert full["exact_values"] == ref["exact_values"]


def test_unbounded_and_strict(neo):
    raw = {"form": "optimisation", "interpretation": "an unbounded objective, precisely stated",
           "universe": {"entity": "loan"}, "variables": [{"name": "x", "sort": "Real"}],
           "constraints": [{"name": "c", "expr": "x >= 0"}], "target": {"objective": "x", "sense": "max"}}
    assert prove(neo, raw)["status"] == "unbounded"
    raw["constraints"] = [{"name": "c", "expr": "x < 5"}]
    with pytest.raises(ProofError, match="strict"):
        prove(neo, raw)


def test_feasibility_core_probes_and_backtest(neo):
    raw = {"form": "feasibility", "interpretation": "healthy interstate target under the deposit cap",
           "universe": {"entity": "bank_quarter"},
           "parameters": {"q": "max_of(quarter)",
                          "D_US": {"expr": "sum(DEPDOM / 1e6, source == 'industry' and quarter == q)", "unit": "USD_billions"},
                          "D_JPM": {"expr": "sum(DEPDOM / 1e6, source == 'holding_company:1039502' and quarter == q)",
                                    "unit": "USD_billions"},
                          "cap": "rule_param('deposit_cap', 'cap')"},
           "variables": [{"name": "D_t", "sort": "Real", "domain": "D_t >= 0", "unit": "USD_billions"},
                         {"name": "failing", "sort": "Bool"}, {"name": "interstate", "sort": "Bool"}],
           "constraints": [{"name": "R1", "expr": "implies(interstate and not failing, D_JPM * 2 + D_t <= cap * D_US)",
                            "source": "rule:deposit_cap"}],
           "assumptions": [{"name": "healthy", "expr": "not failing"}, {"name": "out_of_state", "expr": "interstate"},
                           {"name": "has_deposits", "expr": "D_t > 0"}],
           "target": {"query": "exists"},
           "backtest": {"universe": {"entity": "deal"}, "rows": "generation == 1 and is_assisted_failed", "limit": 50,
                        "bind": {"D_t": "target_deposits_pre_deal_000 / 1e6", "failing": "is_assisted_failed",
                                 "interstate": "target_state != 'NY'"}}}
    p = prove(neo, raw)
    jpm, us = p["parameters"]["D_JPM"], p["parameters"]["D_US"]
    if 2 * jpm > 0.1 * us:
        assert p["status"] == "infeasible" and {"R1", "healthy", "out_of_state"} <= set(p["solver"]["unsat_core"])
        assert {"healthy", "out_of_state"} <= set(p["solver"]["routes_that_open"])
    else:
        assert p["status"] == "feasible"
    assert p["backtest"]["passed"] and p["backtest"]["cases"] > 0


def test_sensitivity_reruns_the_model(neo):
    p = prove(neo, {"form": "value", "interpretation": "loans whose rate exceeds a threshold",
                    "universe": {"entity": "loan"}, "parameters": {"t": {"expr": "500", "unit": "bp"}},
                    "target": {"expr": "count(rate_bp > t)"},
                    "sensitivity": [{"parameter": "t", "values": [200, 500, 1000]}]})
    got = {r["value"]: r["answer"] for r in p["sensitivity"][0]["runs"]}
    for t in (200, 500, 1000):
        assert got[t] == direct(neo, f"SELECT COUNT(*) FROM {{loans}} WHERE rate_bp > {t}")[0][0]


def test_second_engine_when_too_large(neo):
    neo.cfg["verification"]["exact_max_rows"] = 1000
    p = prove(neo, {"form": "value", "interpretation": "total exposure", "universe": {"entity": "loan"},
                    "target": {"expr": "sum(balance_usd)"}})
    assert p["self_check"]["matches"] and p["self_check"]["exact"] is False and "second engine" in p["self_check"]["method"]


def test_a_compiler_bug_is_caught(neo, monkeypatch):
    """Inject a bug into SQL generation (conditional counts ignore their condition): the independent interpreter
    disagrees, so the answer is marked `mismatch` instead of `proved`."""
    from neo.lake.engines import Dialect
    monkeypatch.setattr(Dialect, "count_if", lambda self, cond: "COUNT(*)")
    p = prove(neo, {"form": "value", "interpretation": "number of defaulted loans", "universe": {"entity": "loan"},
                    "target": {"expr": "count(status == 'default')"}})
    assert p["status"] == "mismatch" and p["self_check"]["matches"] is False
