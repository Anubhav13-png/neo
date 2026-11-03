"""Row policies, briefings that do not leak, audit integrity, review routing, budgets and data egress."""
import json

import pytest

from neo.governance.audit import AuditLog
from neo.llm.gateway import ROW_DATA_MARKER, BudgetExceeded, LLMGateway
from neo.llm.providers import LLMError, ScriptedLLM

from conftest import direct

CARDS = {"form": "value", "interpretation": "total exposure on cards, in USD", "universe": {"entity": "loan"},
         "rows": "product == 'Card'", "target": {"columns": {"exposure_usd": "sum(balance_usd)", "n": "count()"}}}


def test_row_policy_limits_the_answer(neo, scripted):
    scripted(neo, CARDS, {"verdict": "ok"})
    r = neo.ask("What is our total exposure on cards?", "ana")
    (exp, n), = direct(neo, "SELECT SUM(balance_usd), COUNT(*) FROM {loans} WHERE product='Card' AND booking_region='EMEA'")
    assert r.status == "proved" and r.proof["answer"] == {"exposure_usd": exp, "n": n}
    assert "Access policy on `loan`" in r.certificate and "EMEA" in r.certificate
    scripted(neo, CARDS, {"verdict": "ok"})
    everyone = neo.ask("What is our total exposure on cards?", "demo")
    assert everyone.proof["answer"]["n"] > n


def test_briefing_hides_counts_for_restricted_rows_and_hidden_fields(neo, scripted):
    w = scripted(neo, CARDS, {"verdict": "ok"})
    neo.ask("What is our total exposure on cards by region?", "ana")
    briefing = w.calls[0]["system"]
    line = next(l for l in briefing.splitlines() if l.strip().startswith("booking_region"))
    assert "'EMEA'" in line and "(" not in line.split("values:")[1]   # loan values listed, whole-table counts withheld
    assert "range" not in next(l for l in briefing.splitlines() if l.strip().startswith("balance_usd"))
    assert "customer_name" not in briefing and "hidden by your access level" in briefing


def test_certificate_for_the_llm_has_no_row_data(neo, scripted):
    scripted(neo, CARDS, {"verdict": "ok"})
    r = neo.ask("What is our total exposure on cards?", "demo")
    llm_cert = neo.runs.load_step(r.run_id, "certify")["llm"]
    assert "withheld from the language model" in llm_cert and "```sql" not in llm_cert
    assert "Sample of eligible rows" in r.certificate


def test_egress_guard_and_budget(neo):
    gw = LLMGateway(neo.cfg, neo.runs, providers={"default": ScriptedLLM(responses=["x"] * 5)})
    with pytest.raises(LLMError, match="row-level data"):
        gw.chat("answer", "system", [{"role": "user", "content": f"{ROW_DATA_MARKER} rows"}])
    p = neo.principal("demo")
    p.daily_token_budget = 1
    neo.runs.log_llm_call("r", "demo", "formalize", "m", 10, 10, 1, "h")
    with pytest.raises(BudgetExceeded):
        gw.chat("formalize", "system", [{"role": "user", "content": "hi"}], principal=p)


def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    for i in range(5):
        log.append("event", "u", n=i)
    assert log.verify() == (True, 5, "5 entries, chain intact")
    lines = open(log.path).read().splitlines()
    entry = json.loads(lines[2])
    entry["data"]["n"] = 99
    lines[2] = json.dumps(entry)
    open(log.path, "w").write("\n".join(lines) + "\n")
    ok, n, msg = log.verify()
    assert not ok and "line 3" in msg


def test_review_routing_and_decision(neo, scripted, monkeypatch):
    from neo.lake.engines import Dialect
    monkeypatch.setattr(Dialect, "count_if", lambda self, cond: "COUNT(*)")
    scripted(neo, {"form": "value", "interpretation": "number of defaulted loans", "universe": {"entity": "loan"},
                   "target": {"expr": "count(status == 'default')"}}, {"verdict": "ok"})
    r = neo.ask("How many loans are in default?", "demo")
    assert r.status == "mismatch" and any("disagrees" in x for x in r.review_reasons)
    pending = [x for x in neo.runs.reviews("pending") if x["run_id"] == r.run_id]
    assert pending
    neo.runs.decide_review(r.run_id, "raj", approve=False, note="compiler bug")
    assert not [x for x in neo.runs.reviews("pending") if x["run_id"] == r.run_id]
    events = [json.loads(l)["event"] for l in open(neo.cfg["governance"]["audit_log"])]
    assert "question_asked" in events and "answer_delivered" in events


def test_access_denial_is_audited(neo, scripted):
    bad = {"form": "value", "interpretation": "count of customers with a given name",
           "universe": {"entity": "customer"}, "target": {"expr": "count(customer_name == 'Customer 7')"}}
    scripted(neo, bad, bad, bad, bad)
    r = neo.ask("How many customers are named Customer 7?", "demo")
    assert r.status == "no_model" and "restricted" in r.answer
    denials = [json.loads(l) for l in open(neo.cfg["governance"]["audit_log"]) if '"access_denied"' in l]
    assert any(d["data"]["run_id"] == r.run_id for d in denials)


def test_single_user_deployment(neo):
    """config/users.yaml has one user with full control; the golden set runs as that user and skips the
    row-policy case for a user this deployment does not have."""
    import os
    from neo.evaluation import run_golden
    from neo.governance.identity import Directory
    from conftest import ROOT
    neo.directory = Directory(os.path.join(ROOT, "config", "users.yaml"))
    assert list(neo.directory.users) == ["admin"] and neo.directory.default_user() == "admin"
    p = neo.principal("admin")
    assert p.clearance == "restricted" and not p.row_policies and p.can_approve and p.large_queries
    rep = run_golden(neo, os.path.join(ROOT, "golden", "core.yaml"))
    assert [s["id"] for s in rep["skipped"]] == ["card_exposure_emea"] and rep["cases"] == 5
    assert {r["run_id"] for r in rep["results"]} and all(neo.runs.run(r["run_id"])["user"] == "admin"
                                                         for r in rep["results"])
