"""Lean checks what Z3 decided: the theorem library builds with no unproved step, and per-run theorems about the
exact formulas Z3 was given are accepted by Lean's kernel (and a false one is rejected)."""
import pytest
import z3

from neo.governance.review import review_reasons
from neo.proof.lean import Goals, STANDARD_AXIOMS, build, check, toolchain

from conftest import ground
from test_prover import FLOOR, prove

pytestmark = pytest.mark.skipif(toolchain({"lean": {"enabled": "auto"}}) is None, reason="Lean is not installed")


@pytest.fixture
def lean_neo(neo):
    neo.cfg["lean"] = dict(neo.cfg["lean"], enabled="required")
    return neo


def theorems(p):
    return {t["name"]: t for t in p["lean"]["theorems"]}


def test_library_builds_with_only_standard_axioms(neo):
    r = build(neo.cfg)
    assert r["ok"], r["output"][-2000:]
    assert all(set(ax) <= set(STANDARD_AXIOMS) for ax in r["axioms"].values())
    assert "Neo.Refinement.optimal_of_subset" in r["axioms"] and "Neo.Extremum.ranking_complete" in r["axioms"]


def test_value_is_checked_by_the_kernel(lean_neo):
    p = prove(lean_neo, {"form": "value", "interpretation": "failed-bank deals in the 1980s: branches and average assets",
                         "universe": {"entity": "deal"}, "rows": "is_assisted_failed and 1980 <= year <= 1989",
                         "target": {"columns": {"branches": "sum(branches_transferred)",
                                                "avg_assets": "avg(target_assets_pre_deal_000)"}},
                         "grounding": ground(lean_neo)})
    assert p["lean"]["status"] == "checked", p["lean"]
    assert all(t["result"] == "checked" and t["kind"] == "compute" for t in p["lean"]["theorems"])
    assert "Neo.Exact.unique_of_unsat" in {c["theorem"] for c in p["lean"]["cites"]}
    step = p["verification"][-1]
    assert step["step"].startswith("Lean") and step["result"] == "proved"


def test_refinement_optimum_bound_and_witness(lean_neo):
    lean_neo.cfg["proof"].update(symbolic_max_rows=100, cegar_seed_rows=20, cegar_batch=20)
    p = prove(lean_neo, FLOOR)
    assert p["status"] == "optimal" and p["solver"]["mode"] == "cegar"
    th = theorems(p)
    assert th["bound"]["kind"] == "symbolic" and th["bound"]["result"] == "checked"
    assert th["bound"]["facts"] <= 3                                     # the binding targets only
    assert any(n.startswith("witness") for n in th) and p["lean"]["status"] in ("checked", "partial")
    assert all(t["result"] == "checked" for t in th.values())
    assert "∀ (v_r : Rat)" in p["lean"]["source"]
    assert "Neo.Refinement.optimal_of_subset" in {c["theorem"] for c in p["lean"]["cites"]}


def test_infeasible_clash_and_global_claim(lean_neo):
    base = {"universe": {"entity": "deal"}, "grounding": ground(lean_neo),
            "rows": "generation == 1 and not_null(target_assets_pre_deal_000)",
            "variables": [{"name": "x", "sort": "Real"}]}
    p = prove(lean_neo, {**base, "form": "feasibility", "interpretation": "a cap below every direct target's assets",
                         "constraints": [{"name": "covers", "expr": "all_rows(x >= target_assets_pre_deal_000)"},
                                         {"name": "small", "expr": "x <= 0"}],
                         "target": {"query": "exists"}})
    assert p["status"] == "infeasible" and theorems(p)["clash"]["result"] == "checked"
    q = prove(lean_neo, {**base, "form": "check", "interpretation": "x above every direct target's assets is above one",
                         "constraints": [{"name": "covers", "expr": "all_rows(x >= target_assets_pre_deal_000)"}],
                         "target": {"scope": "global", "claim": "x >= 1"}})
    assert q["status"] == "proved" and theorems(q)["claim"]["result"] == "checked"


def test_a_false_fact_is_rejected_and_routed(neo):
    neo.cfg["lean"] = dict(neo.cfg["lean"], enabled="required")
    g = Goals(neo.cfg)
    g.compute("values", "a deliberately wrong value", [z3.RealVal("1/3") + z3.RealVal("1/6") == z3.RealVal("1/3")])
    r = check(g, neo.cfg, "test")
    assert r["status"] == "failed" and r["theorems"][0]["result"] == "rejected"
    reasons = review_reasons(neo.cfg, {"status": "proved", "lean": r}, None, {}, [])
    assert any("Lean" in x for x in reasons)


def test_off_means_no_certificate_line(neo):
    p = prove(neo, {"form": "value", "interpretation": "total pre-deal assets over every target",
                    "universe": {"entity": "deal"}, "target": {"expr": "sum(target_assets_pre_deal_000)"},
                    "grounding": ground(neo)})
    assert p["lean"]["status"] == "not_checked" and not any(s["step"].startswith("Lean") for s in p["verification"])


def test_claim_theorems_fidelity_rounding_and_plan(lean_neo):
    from conftest import script
    from test_verify import PLAN, Q, REQS, count_model
    script(lean_neo, count_model(lean_neo), {"verdict": "ok"}, requirements=REQS,
           answer='{"text": "There were {{answer}} failed-bank deals out of {{universe_rows}}.", '
                  '"claims": [{"id": "A1", "kind": "comparison", "text": "out of", "left": "answer", "op": "<", '
                  '"right": "universe_rows"}]}')
    r = lean_neo.ask(Q, "demo")
    led = lean_neo.runs.load_step(r.run_id, "ledger")
    th = {t["name"]: t for t in led["lean"]["theorems"]}
    assert led["lean"]["status"] == "checked", led["lean"]
    assert any(n.startswith("fidelity_R2") for n in th) and any(n.startswith("rounds_slot_universe_rows") for n in th)
    assert "compare_A1" in th and all(t["result"] == "checked" for t in th.values()), sorted(th)
    assert {"Neo.Fidelity.kept_meet", "Neo.Rounding.shows_of_bounds"} <= {c["theorem"] for c in led["lean"]["cites"]}
    script(lean_neo, {**PLAN, "grounding": ground(lean_neo)}, {"verdict": "ok"})
    p = lean_neo.ask("Were Texas failed-bank targets bigger than voluntary ones in the 1980s?", "demo").proof
    names = {t["name"]: t["result"] for t in p["lean"]["theorems"]}
    assert p["lean"]["status"] == "checked" and names.get("combine") == "checked"
    assert "Neo.Compose.combine_value" in {c["theorem"] for c in p["lean"]["cites"]}
