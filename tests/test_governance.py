"""Row policies, briefings that do not leak, audit integrity, review routing, budgets and data egress."""
import json

import pytest

from neo.governance.audit import AuditLog
from neo.llm.gateway import ROW_DATA_MARKER, BudgetExceeded, LLMGateway
from neo.llm.providers import LLMError, ScriptedLLM

from conftest import direct

from conftest import ground


def failed(neo) -> dict:
    return {"form": "value", "interpretation": "number of failed-bank deals and branches transferred",
            "universe": {"entity": "deal"}, "rows": "is_assisted_failed",
            "target": {"columns": {"n": "count()", "branches": "sum(branches_transferred)"}}, "grounding": ground(neo)}


def test_row_policy_limits_the_answer(neo, scripted):
    scripted(neo, failed(neo), {"verdict": "ok"})
    r = neo.ask("How many failed-bank deals were there?", "ana")
    (n,), = direct(neo, "SELECT COUNT(*) FROM {deals} WHERE deal_category = 'Assisted / failed-bank deal' "
                        "AND target_state = 'TX'")
    assert r.status == "proved" and r.proof["answer"]["n"] == n > 0
    assert "Access policy on `deal`" in r.certificate and "TX" in r.certificate
    scripted(neo, failed(neo), {"verdict": "ok"})
    everyone = neo.ask("How many failed-bank deals were there?", "demo")
    assert everyone.proof["answer"]["n"] > n


def test_briefing_hides_counts_for_restricted_rows_and_hidden_fields(neo, scripted):
    w = scripted(neo, failed(neo), {"verdict": "ok"})
    neo.ask("How many failed-bank deals were there, by deal category?", "ana")
    briefing = w.calls[0]["system"]
    line = next(l for l in briefing.splitlines() if l.strip().startswith("deal_category"))
    assert "'Voluntary M&A'" in line and "(" not in line.split("values:")[1]   # values listed, whole-table counts withheld
    assert "range" not in next(l for l in briefing.splitlines() if l.strip().startswith("target_assets_pre_deal_000"))
    assert "acquirer_website" not in briefing and "hidden by your access level" in briefing


def test_certificate_for_the_llm_has_no_row_data(neo, scripted):
    scripted(neo, failed(neo), {"verdict": "ok"})
    r = neo.ask("How many failed-bank deals were there?", "demo")
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
    scripted(neo, {"form": "value", "interpretation": "number of failed-bank deals", "universe": {"entity": "deal"},
                   "target": {"expr": "count(is_assisted_failed)"}, "grounding": ground(neo)}, {"verdict": "ok"})
    r = neo.ask("How many failed-bank deals were there?", "demo")
    assert r.status == "mismatch" and any("disagrees" in x for x in r.review_reasons)
    pending = [x for x in neo.runs.reviews("pending") if x["run_id"] == r.run_id]
    assert pending
    neo.runs.decide_review(r.run_id, "raj", approve=False, note="compiler bug")
    assert not [x for x in neo.runs.reviews("pending") if x["run_id"] == r.run_id]
    events = [json.loads(l)["event"] for l in open(neo.cfg["governance"]["audit_log"])]
    assert "question_asked" in events and "answer_delivered" in events


def test_access_denial_is_audited(neo, scripted):
    bad = {"form": "value", "interpretation": "count of buyers with a given website",
           "universe": {"entity": "deal"}, "target": {"expr": "count(acquirer_website == 'www.jpmorganchase.com')"},
           "grounding": ground(neo)}
    scripted(neo, bad, bad, bad, bad)
    r = neo.ask("How many deals were bought by a bank with the website www.jpmorganchase.com?", "demo")
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
    assert [s["id"] for s in rep["skipped"]] == ["failed_deals_in_state"] and rep["cases"] == 6
    assert {r["run_id"] for r in rep["results"]} and all(neo.runs.run(r["run_id"])["user"] == "admin"
                                                         for r in rep["results"])
