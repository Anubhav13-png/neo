"""Every form is proved, and every answer equals an independent computation (hand-written SQL or plain Python)."""
from fractions import Fraction

import pytest

from neo.proof.prover import ProofError, Prover
from neo.semantic.validator import ValidationContext, validate

from conftest import direct, ground


def prove(neo, raw, user="admin", question="q"):
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    res = validate(raw, ValidationContext(onto, question, neo.principal(user), neo.rules, neo.catalog, snap, neo.cfg))
    assert not res.errors, res.errors
    return Prover(onto, res.model, neo.engine.bind(snap), neo.cfg, neo.rules).run()


def exact(p, key):
    return Fraction(p["exact_values"][key]) if key in p.get("exact_values", {}) else Fraction(p["answer"][key])


def test_value_columns(neo):
    p = prove(neo, {"form": "value", "interpretation": "failed-bank deals: branches, average pre-deal assets, share in-state",
                    "universe": {"entity": "deal"}, "rows": "is_assisted_failed",
                    "target": {"columns": {"branches": "sum(branches_transferred)",
                                           "avg_assets": "avg(target_assets_pre_deal_000)",
                                           "in_state": "share(same_state)", "n": "count()"}},
                    "grounding": ground(neo)})
    rows = direct(neo, "SELECT branches_transferred, target_assets_pre_deal_000, same_state FROM {deals} "
                       "WHERE deal_category = 'Assisted / failed-bank deal'")
    assets = [Fraction(repr(r[1])) for r in rows if r[1] is not None]
    assert p["status"] == "proved" and p["self_check"]["matches"] and p["exactness"]["exact"]
    assert p["answer"]["branches"] == sum(Fraction(repr(r[0])) for r in rows if r[0] is not None)
    assert p["answer"]["n"] == len(rows) == 238
    assert exact(p, "avg_assets") == sum(assets) / len(assets)
    assert exact(p, "in_state") == Fraction(sum(1 for r in rows if r[2]), len(rows))


def test_table_with_join(neo):
    p = prove(neo, {"form": "table", "interpretation": "per generation, deals and targets in their buyer's state",
                    "universe": {"entity": "deal", "joins": [{"relation": "rel_deal_buyer", "as": "buyer"}]},
                    "group_by": "generation",
                    "target": {"columns": {"n": "count()", "same": "count(buyer.target_state == target_state)"}},
                    "grounding": ground(neo)})
    want = {g: (n, s) for g, n, s in direct(
        neo, "SELECT d.generation, COUNT(*), COUNT(*) FILTER (WHERE b.target_state = d.target_state) "
             "FROM {deals} d LEFT JOIN {deals} b ON d.acquirer_cert = b.target_cert GROUP BY 1")}
    assert p["status"] == "proved" and p["self_check"]["matches"]
    assert {r["group"]: (r["n"], r["same"]) for r in p["answer"]} == want


def test_extremum_rows_and_groups(neo):
    p = prove(neo, {"form": "extremum", "interpretation": "three largest deals by pre-deal assets in $B",
                    "universe": {"entity": "deal"}, "target": {"expr": "target_assets_pre_deal_000 / 1e6",
                                                               "sense": "max", "top": 3}, "grounding": ground(neo)})
    want = [r[0] for r in direct(neo, "SELECT target_cert FROM {deals} WHERE target_assets_pre_deal_000 IS NOT NULL "
                                      "ORDER BY target_assets_pre_deal_000 DESC LIMIT 3")]
    assert p["status"] == "proved" and [r["target_cert"] for r in p["answer"]] == want
    assert all(r["rows_beating_it"] == i for i, r in enumerate(p["answer"]))
    g = prove(neo, {"form": "extremum", "interpretation": "state with the most acquired banks",
                    "universe": {"entity": "deal"}, "group_by": "target_state",
                    "target": {"expr": "count()", "sense": "max"}, "grounding": ground(neo)})
    (st, n), = direct(neo, "SELECT target_state, COUNT(*) FROM {deals} GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    assert g["answer"]["group"] == st and g["answer"]["value"] == n and g["self_check"]["matches"]


def test_set_and_check(neo):
    s = prove(neo, {"form": "set", "interpretation": "failed-bank deals in Texas",
                    "universe": {"entity": "deal"}, "target": {"select": ["target_name"], "limit": 5,
                    "where": "is_assisted_failed and target_state == 'TX'"}, "grounding": ground(neo)})
    (n,), = direct(neo, "SELECT COUNT(*) FROM {deals} WHERE deal_category = 'Assisted / failed-bank deal' "
                        "AND target_state = 'TX'")
    assert s["answer"]["count"] == n > 0 and s["self_check"]["matches"]
    c = prove(neo, {"form": "check", "interpretation": "every deal of generation 1 has acquirer cert 628",
                    "universe": {"entity": "deal"},
                    "target": {"scope": "rows", "claim": "(generation == 1) == (acquirer_cert == 628)"},
                    "grounding": ground(neo)})
    assert c["status"] == "proved" and c["self_check"]["matches"]
    r = prove(neo, {"form": "check", "interpretation": "every deal is at most three links from JPMorgan",
                    "universe": {"entity": "deal"}, "target": {"scope": "rows", "claim": "generation <= 3"},
                    "grounding": ground(neo)})
    (bad,), = direct(neo, "SELECT COUNT(*) FROM {deals} WHERE generation > 3")
    assert r["status"] == "refuted" and str(bad) in r["answer"] and r["evidence"]["counterexamples"]


def capital(neo, where="TRUE"):
    """Every target's pre-deal equity / assets, sorted: the ground truth for the floor below."""
    return sorted(Fraction(repr(e)) / Fraction(repr(a)) for e, a in direct(
        neo, f"SELECT target_equity_pre_deal_000, target_assets_pre_deal_000 FROM {{deals}} "
             f"WHERE target_equity_pre_deal_000 IS NOT NULL AND target_assets_pre_deal_000 > 0 AND {where}"))


FLOOR = {"form": "optimisation", "interpretation": "the highest equity-to-assets floor that every target meets",
         "universe": {"entity": "deal"},
         "rows": "not_null(target_equity_pre_deal_000) and target_assets_pre_deal_000 > 0",
         "variables": [{"name": "r", "sort": "Real", "domain": "r >= -100", "unit": "ratio"}],
         "constraints": [{"name": "every_target", "expr": "all_rows(target_equity_pre_deal_000 / target_assets_pre_deal_000 >= r)"}],
         "target": {"objective": "r", "sense": "max"},
         "grounding": {"constraints_not_applicable": [{"id": "con_units", "why": "a ratio of two thousands columns"},
                                                      {"id": "con_blank_numeric_sorting", "why": "blanks are filtered"}]}}


def test_floor_by_refinement_on_every_target(neo):
    """The highest capital ratio every target met: forced past the direct limit, so it is solved by
    counterexample-guided refinement."""
    neo.cfg["proof"].update(symbolic_max_rows=100, cegar_seed_rows=20, cegar_batch=20)
    p = prove(neo, FLOOR)
    assert p["status"] == "optimal" and p["solver"]["mode"] == "cegar"
    assert Fraction(p["exact_values"]["objective"]) == capital(neo)[0]
    q = prove(neo, {**FLOOR, "constraints": [{"name": "at_most_10_below",
                                              "expr": "count(target_equity_pre_deal_000 / target_assets_pre_deal_000 < r) <= 10"}]})
    assert q["solver"]["mode"] == "cegar" and q["solver"]["rounds"] >= 2       # violating targets were added
    assert Fraction(q["exact_values"]["objective"]) == capital(neo)[10]


def test_floor_direct_matches_refinement(neo):
    raw = {**FLOOR, "rows": "is_assisted_failed and not_null(target_equity_pre_deal_000) and target_assets_pre_deal_000 > 0",
           "grounding": {"constraints_not_applicable": FLOOR["grounding"]["constraints_not_applicable"] +
                         [{"id": "con_failed_bank_definition", "why": "uses the deal_category flag"}]}}
    full = prove(neo, raw)
    neo.cfg["proof"].update(symbolic_max_rows=10, cegar_seed_rows=5)
    ref = prove(neo, raw)
    assert full["solver"]["mode"] == "full" and ref["solver"]["mode"] == "cegar"
    assert full["exact_values"] == ref["exact_values"]
    assert Fraction(full["exact_values"]["objective"]) == capital(neo, "deal_category = 'Assisted / failed-bank deal'")[0]


def test_unbounded_and_strict(neo):
    raw = {"form": "optimisation", "interpretation": "an unbounded objective, precisely stated",
           "universe": {"entity": "deal"}, "variables": [{"name": "x", "sort": "Real"}],
           "constraints": [{"name": "c", "expr": "x >= 0"}], "target": {"objective": "x", "sense": "max"}}
    assert prove(neo, raw)["status"] == "unbounded"
    raw["constraints"] = [{"name": "c", "expr": "x < 5"}]
    with pytest.raises(ProofError, match="strict"):
        prove(neo, raw)


def test_feasibility_core_probes_and_backtest(neo):
    """Under the deposit cap, with JPMorgan's largest pre-deal deposits from the data ($2,472.9B) and a stated US total of
    $30T: 10% is $3T, so there is room."""
    raw = {"form": "feasibility", "interpretation": "healthy interstate target under the deposit cap",
           "universe": {"entity": "deal"},
           "parameters": {"D_US": {"expr": "30000", "unit": "USD_billions"},
                          "D_JPM": {"expr": "max_of(acquirer_deposits_pre_deal_000 / 1e6, acquirer_cert == 628)",
                                    "unit": "USD_billions"},
                          "cap": "rule_param('deposit_cap', 'cap')"},
           "variables": [{"name": "D_t", "sort": "Real", "domain": "D_t >= 0", "unit": "USD_billions"},
                         {"name": "failing", "sort": "Bool"}, {"name": "interstate", "sort": "Bool"}],
           "constraints": [{"name": "R1", "expr": "implies(interstate and not failing, D_JPM + D_t <= cap * D_US)",
                            "source": "rule:deposit_cap"}],
           "assumptions": [{"name": "healthy", "expr": "not failing"}, {"name": "out_of_state", "expr": "interstate"},
                           {"name": "has_deposits", "expr": "D_t > 0"}],
           "target": {"query": "exists"}, "grounding": ground(neo),
           "backtest": {"universe": {"entity": "deal"}, "rows": "generation == 1 and is_assisted_failed", "limit": 50,
                        "bind": {"D_t": "target_deposits_pre_deal_000 / 1e6", "failing": "is_assisted_failed",
                                 "interstate": "target_state != 'NY'"}}}
    p = prove(neo, raw)
    (jpm,), = direct(neo, "SELECT MAX(acquirer_deposits_pre_deal_000) / 1e6 FROM {deals} WHERE acquirer_cert = 628")
    assert abs(p["parameters"]["D_JPM"] - jpm) < 1e-6
    assert p["status"] == "feasible"
    assert p["backtest"]["passed"] and p["backtest"]["cases"] > 0


def test_infeasible_core_and_probes(neo):
    raw = {"form": "feasibility", "interpretation": "a stated US total of $17T: 10% is $1.7T, below JPMorgan's $2.47T, so no room",
           "universe": {"entity": "deal"},
           "parameters": {"D_US": {"expr": "17000", "unit": "USD_billions"},
                          "D_JPM": {"expr": "max_of(acquirer_deposits_pre_deal_000 / 1e6, acquirer_cert == 628)",
                                    "unit": "USD_billions"},
                          "cap": "rule_param('deposit_cap', 'cap')"},
           "variables": [{"name": "D_t", "sort": "Real", "domain": "D_t >= 0", "unit": "USD_billions"},
                         {"name": "failing", "sort": "Bool"}, {"name": "interstate", "sort": "Bool"}],
           "constraints": [{"name": "R1", "expr": "implies(interstate and not failing, D_JPM + D_t <= cap * D_US)",
                            "source": "rule:deposit_cap"}],
           "assumptions": [{"name": "healthy", "expr": "not failing"}, {"name": "out_of_state", "expr": "interstate"},
                           {"name": "has_deposits", "expr": "D_t > 0"}],
           "target": {"query": "exists"}, "grounding": ground(neo)}
    p = prove(neo, raw)
    assert p["status"] == "infeasible" and {"R1", "healthy", "out_of_state"} <= set(p["solver"]["unsat_core"])
    assert {"healthy", "out_of_state"} <= set(p["solver"]["routes_that_open"])


def test_sensitivity_reruns_the_model(neo):
    p = prove(neo, {"form": "value", "interpretation": "targets whose pre-deal assets exceed a threshold",
                    "universe": {"entity": "deal"},
                    "parameters": {"t": {"expr": "1000000", "unit": "USD_thousands"}},
                    "target": {"expr": "count(target_assets_pre_deal_000 > t)"}, "grounding": ground(neo),
                    "sensitivity": [{"parameter": "t", "values": [0, 1000000, 10000000]}]})
    got = {r["value"]: r["answer"] for r in p["sensitivity"][0]["runs"]}
    for t in (0, 1000000, 10000000):
        assert got[t] == direct(neo, f"SELECT COUNT(*) FROM {{deals}} WHERE target_assets_pre_deal_000 > {t}")[0][0]


def test_second_engine_when_too_large(neo):
    neo.cfg["verification"]["exact_max_rows"] = 100
    p = prove(neo, {"form": "value", "interpretation": "total pre-deal assets over every target",
                    "universe": {"entity": "deal"}, "target": {"expr": "sum(target_assets_pre_deal_000)"},
                    "grounding": ground(neo)})
    assert p["self_check"]["matches"] and p["self_check"]["exact"] is False and "second engine" in p["self_check"]["method"]


def test_a_compiler_bug_is_caught(neo, monkeypatch):
    """Inject a bug into SQL generation (conditional counts ignore their condition): the independent interpreter
    disagrees, so the answer is marked `mismatch` instead of `proved`."""
    from neo.lake.engines import Dialect
    monkeypatch.setattr(Dialect, "count_if", lambda self, cond: "COUNT(*)")
    p = prove(neo, {"form": "value", "interpretation": "number of failed-bank deals", "universe": {"entity": "deal"},
                    "target": {"expr": "count(is_assisted_failed)"}, "grounding": ground(neo)})
    assert p["status"] == "mismatch" and p["self_check"]["matches"] is False
