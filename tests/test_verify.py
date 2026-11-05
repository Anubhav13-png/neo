"""Every claim is checked: the requirements against the model (fidelity), plans of small models, the written
answer's claims, and the claim ledger that routes the run."""
import pytest

from neo.verify.answer import AnswerCheck, finalize, parse_reply
from neo.verify.rowlogic import RowLogic, implies
from neo.verify.values import _entry

from conftest import direct, ground, script

Q = "How many failed-bank deals happened in the 1980s?"
REQS = [{"id": "R1", "kind": "universe", "text": "the rows are deals", "entity": "deal"},
        {"id": "R2", "kind": "filter", "text": "only failed-bank deals",
         "expr": "deal_category == 'Assisted / failed-bank deal'"},
        {"id": "R3", "kind": "filter", "text": "in the 1980s", "expr": "year >= 1980 and year < 1990"},
        {"id": "R4", "kind": "measure", "text": "a count", "aggregate": "count"}]


def count_model(neo, rows="is_assisted_failed and 1980 <= year <= 1989", **over):
    return {"form": "value", "interpretation": "count of failed-bank deals in the 1980s", "universe": {"entity": "deal"},
            "rows": rows, "target": {"expr": "count()"}, "grounding": ground(neo), **over}


def results(r):
    return {x["id"]: x for x in r.proof["fidelity"]["results"]}


def test_rowlogic_expands_ontology_expressions(neo):
    onto = neo.ontology.load(None, neo.catalog.snapshot())
    lg = RowLogic(onto, "deal")
    a = lg.formula("is_assisted_failed and 1980 <= year <= 1989")
    b = lg.formula("deal_category == 'Assisted / failed-bank deal' and year >= 1980 and year < 1990")
    assert implies(a, b) is True                      # 1989 <= year < 1990 for whole-number years is not needed:
    assert implies(b, a) is False                     # year is a number, so 1989.5 separates them symbolically
    assert implies(lg.formula("target_state in ['TX', 'IL']"), lg.formula("target_state != 'NY'")) is False


def test_fidelity_matches_and_proves(neo):
    script(neo, count_model(neo), {"verdict": "ok"}, requirements=REQS)
    r = neo.ask(Q, "demo")
    res = results(r)
    assert r.status == "proved" and r.proof["answer"] == 132 and r.attempts == 1
    assert {k: v["result"] for k, v in res.items()} == {"R1": "matched", "R2": "matched", "R3": "matched",
                                                        "R4": "matched", "R2+R3": "matched"}
    assert res["R2"]["counts"]["kept_but_excluded"] == 0 and "Z3" in res["R2"]["symbolic"]
    assert r.proof["fidelity"]["status"] == "matched" and len(r.proof["fidelity"]["lean_goals"]) >= 2
    assert "### What the question requires" in r.certificate and "### Claim ledger" in r.certificate


def test_fidelity_failure_goes_back_to_the_writer(neo):
    w = script(neo, count_model(neo, rows="is_assisted_failed and 1980 <= year <= 1990"), count_model(neo),
               {"verdict": "ok"}, requirements=REQS)
    r = neo.ask(Q, "demo")
    assert r.status == "proved" and r.attempts == 2 and r.proof["answer"] == 132
    first = neo.runs.read(r.run_id, "attempts/attempt_1.json")
    assert any("R3" in e and "excludes" in e for e in first["errors"])
    assert "does not answer the question as asked" in w.calls[1]["messages"][-1]["content"]
    (n1990,), = direct(neo, "SELECT COUNT(*) FROM {deals} WHERE deal_category = 'Assisted / failed-bank deal' "
                            "AND year = 1990")
    assert first["fidelity"]["results"][2]["counts"]["kept_but_excluded"] == n1990 > 0


def test_narrower_for_missing_values_is_accepted_but_not_otherwise(neo):
    script(neo, count_model(neo, rows="is_assisted_failed and 1980 <= year <= 1989 and "
                                      "not_null(target_assets_pre_deal_000)"), {"verdict": "ok"}, requirements=REQS)
    r = neo.ask(Q, "demo")
    assert r.attempts == 1 and results(r)["R2+R3"]["result"] == "narrower"
    assert r.proof["fidelity"]["status"] == "narrower"
    wrong = count_model(neo, rows="is_assisted_failed and 1980 <= year <= 1989 and target_state == 'TX'")
    script(neo, wrong, wrong, wrong, wrong, {"verdict": "ok"}, requirements=REQS)
    r = neo.ask(Q, "demo")
    assert r.attempts == 4 and results(r)["R2+R3"]["result"] == "missing"       # accepted on the last try ...
    assert any("requirement" in x for x in r.review_reasons)                    # ... but sent to review


def test_measure_and_universe_mismatch(neo):
    reqs = REQS[:1] + [{"id": "R2", "kind": "measure", "text": "an average", "aggregate": "avg",
                        "of": "target_assets_pre_deal_000"}]
    m = count_model(neo, rows="is_assisted_failed", target={"expr": "sum(target_assets_pre_deal_000)"})
    script(neo, m, m, m, m, {"verdict": "ok"}, requirements=reqs)
    r = neo.ask("What was the average size of failed-bank targets?", "demo")
    assert results(r)["R2"]["result"] == "contradicted" and "computes sum" in results(r)["R2"]["detail"]


PLAN = {"form": "plan", "interpretation": "average pre-deal assets of Texas 1980s targets, failed against voluntary",
        "nodes": {"N1": {"universe": {"entity": "deal"}, "target": {"expr": "avg(target_assets_pre_deal_000 / 1000)"},
                         "rows": "is_assisted_failed and target_state == 'TX' and 1980 <= year <= 1989 and "
                                 "not_null(target_assets_pre_deal_000)"},
                  "N2": {"universe": {"entity": "deal"}, "target": {"expr": "avg(target_assets_pre_deal_000 / 1000)"},
                         "rows": "deal_category == 'Voluntary M&A' and target_state == 'TX' and 1980 <= year <= 1989 "
                                 "and not_null(target_assets_pre_deal_000)"}},
        "combine": {"columns": {"failed_bigger": "N1 > N2", "ratio": "N1 / N2"}}}


def test_plan_proves_each_part_and_combines(neo):
    plan = {**PLAN, "grounding": ground(neo)}
    reqs = [{"id": "R1", "kind": "filter", "text": "Texas", "expr": "target_state == 'TX'"},
            {"id": "R2", "kind": "measure", "text": "average size", "aggregate": "avg", "of": "target_assets_pre_deal_000"}]
    script(neo, plan, {"verdict": "ok"}, requirements=reqs)
    r = neo.ask("Were failed-bank targets in Texas in the 1980s bigger on average than voluntary ones?", "demo")
    want = dict(direct(neo, "SELECT deal_category, AVG(target_assets_pre_deal_000) / 1000 FROM {deals} WHERE "
                            "target_state = 'TX' AND year BETWEEN 1980 AND 1989 AND target_assets_pre_deal_000 IS NOT "
                            "NULL GROUP BY 1"))
    a = r.proof["answer"]
    assert r.status == "proved" and r.proof["form"] == "plan"
    assert abs(a["N1"] - want["Assisted / failed-bank deal"]) < 1e-6 and abs(a["N2"] - want["Voluntary M&A"]) < 1e-6
    assert a["failed_bigger"] is True and abs(a["ratio"] - a["N1"] / a["N2"]) < 1e-6
    assert all(n["self_check"]["matches"] for n in r.proof["nodes"].values())
    assert {x["result"] for x in r.proof["fidelity"]["results"] if x["id"] == "R1"} == {"matched"}
    assert "### Plan" in r.certificate


def test_plan_errors_name_the_node(neo):
    bad = {**PLAN, "grounding": ground(neo),
           "nodes": {**PLAN["nodes"], "N2": {**PLAN["nodes"]["N2"], "rows": "target_stat == 'TX'"}}}
    w = script(neo, bad, {**PLAN, "grounding": ground(neo)}, {"verdict": "ok"})
    r = neo.ask("Compare Texas failed and voluntary targets", "demo")
    assert r.attempts == 2 and r.status == "proved"
    assert "node N2" in w.calls[1]["messages"][-1]["content"]


def checker(cat, owed=(), cert=""):
    return AnswerCheck(cat, "q", "", cert, [{"id": "R2", "result": "matched"}], list(owed), {"lean": {}})


def test_answer_slots_numbers_and_claims():
    cat = {"answer": _entry(132, "count"), "eligible_rows": _entry(955, "count"),
           "rank1.value": _entry(307.021614, "USD billions")}
    rep = checker(cat).check(parse_reply(
        '{"text": "There were {{answer}} deals out of {{eligible_rows}}; the largest target had $307.0 billion. '
        'That is 13.8% of all deals. Most were in Texas with 41 banks.", "claims": ['
        '{"id": "A1", "kind": "derived", "text": "13.8%", "expr": "answer / eligible_rows", "shown": "13.8%"},'
        '{"id": "A2", "kind": "comparison", "text": "largest", "left": "rank1.value", "op": ">", "right": 300},'
        '{"id": "A3", "kind": "scope", "text": "deals", "requirements": ["R2"]}]}'))
    by = {c["id"]: c["result"] for c in rep["claims"]}
    assert rep["text"].startswith("There were 132 deals out of 955;")
    assert by["A1"] == by["A2"] == by["A3"] == "true"
    assert [c["text"] for c in rep["claims"] if c["result"] == "unsupported"] == ["41"]
    assert {g["name"].split("_")[0] for g in rep["lean_goals"]} >= {"rounds", "derived", "compare"}
    final = finalize(rep, {})
    assert "Texas" not in final["text"] and "removed 1 sentence" in final["text"]


def test_wrong_number_is_repaired_then_removed(neo):
    neo.cfg["pipeline"]["answer_repairs"] = 1
    good = '{"text": "There were {{answer}} failed-bank deals in the 1980s.", "claims": []}'
    script(neo, count_model(neo), {"verdict": "ok"}, answer=['{"text": "There were 123 deals.", "claims": []}', good])
    r = neo.ask(Q, "demo")
    assert r.answer == "There were 132 failed-bank deals in the 1980s."
    steps = neo.runs.load_step(r.run_id, "answer")
    assert len(steps["drafts"]) == 2 and "123" in steps["drafts"][0]["problems"][0]
    script(neo, count_model(neo), {"verdict": "ok"}, answer=['{"text": "Deals: 123. Done.", "claims": []}'] * 2)
    r = neo.ask(Q, "demo")
    assert "123" not in r.answer and "removed 1 sentence" in r.answer
    assert any("answer check removed" in x for x in r.review_reasons)


def test_owed_caveat_is_added_from_the_ontology(neo):
    m = count_model(neo, rows="True", target={"expr": "avg(deal_price_usd)"})
    script(neo, m, {"verdict": "ok"}, answer=['{"text": "The average price was ${{answer|millions|1}} million.", '
                                              '"claims": []}'] * 3)
    r = neo.ask("What was the average deal price?", "demo")
    assert "con_price_synthetic" in r.answer and "random" in r.answer.lower()
    led = neo.runs.load_step(r.run_id, "ledger")
    assert any(row["id"] == "caveat:con_price_synthetic" and row["result"] == "added" for row in led["rows"])


def test_plain_text_answers_still_get_their_numbers_checked(neo):
    script(neo, count_model(neo), {"verdict": "ok"},
           answer="There were 132 such deals, about 14% of the 955. Texas had 41 of them.")
    r = neo.ask(Q, "demo")
    led = neo.runs.load_step(r.run_id, "ledger")
    nums = {row["text"]: row["result"] for row in led["rows"] if row["source"] == "answer"}
    assert nums["132"] == "true" and nums["955"] == "true" and nums["14%"] == "true"     # 132 / 955 = 13.8%
    assert nums["41"] == "removed" and "Texas" not in r.answer and "132 such deals" in r.answer
