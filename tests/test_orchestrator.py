"""The durable workflow: repair, clarification, review, crash recovery, replay, the job queue and the publish gate."""
import os

import pytest

from neo.bootstrap import init_demo
from neo.evaluation import publish_ontology, run_golden
from neo.llm.gateway import LLMGateway
from neo.llm.providers import ScriptedLLM
from neo.orchestrator import Neo

from conftest import FDIC_CACHE, ROOT, copy_ontology, direct, ground, make_cfg, script

Q = "How many branches were transferred in failed-bank deals?"


def branches(neo, **over) -> dict:
    return {"form": "value", "interpretation": "total branches transferred in failed-bank deals",
            "universe": {"entity": "deal"}, "rows": "is_assisted_failed",
            "target": {"expr": "sum(branches_transferred)"}, "grounding": ground(neo), **over}


def test_repair_loop(neo, scripted):
    w = scripted(neo, branches(neo, target={"expr": "sum(branches_transfered)"}), branches(neo), {"verdict": "ok"})
    r = neo.ask(Q, "demo")
    assert r.status == "proved" and r.attempts == 2
    assert "Did you mean: branches_transferred" in w.calls[1]["messages"][-1]["content"]
    assert neo.runs.read(r.run_id, "attempts/attempt_1.json")["errors"]


def test_clarification_when_nothing_links(neo, scripted):
    scripted(neo)
    r = neo.ask("Will it rain in Paris tomorrow afternoon?", "demo")
    assert r.status == "needs_clarification" and "Which of these" in r.answer
    assert any(c["kind"] == "clarification" for c in neo.runs.curation_summary())


def test_not_expressible(neo, scripted):
    w = scripted(neo, {"form": "not_expressible", "interpretation": "Motives are not in the data; this needs "
                                                                    "board minutes, not deal records."},
                 answer="The data cannot answer why.")
    r = neo.ask("Why did JPMorgan's management favour failed-bank deals?", "demo")
    assert r.status == "not_expressible" and len(w.calls) == 1          # no review call for not_expressible


def test_gives_up_cleanly(neo, scripted):
    scripted(neo, *["not json"] * 4)
    r = neo.ask(Q, "demo")
    assert r.status == "no_model" and "Could not build a valid model" in r.answer


def test_review_revision_is_validated_and_proved(neo, scripted):
    wrong = branches(neo, rows="not is_assisted_failed")
    scripted(neo, wrong, {"verdict": "revise", "issues": ["voluntary deals counted"], "model": branches(neo)})
    r = neo.ask(Q, "demo")
    (want,), = direct(neo, "SELECT SUM(branches_transferred) FROM {deals} "
                           "WHERE deal_category = 'Assisted / failed-bank deal'")
    assert r.proof["answer"] == want and r.model["rows"] == "is_assisted_failed"


def test_resume_after_a_crash_uses_checkpoints(neo):
    writer = ScriptedLLM(responses=[branches(neo), {"verdict": "ok"}])
    neo.gateway = LLMGateway(neo.cfg, neo.runs, providers={"default": writer, "formalize": writer, "review": writer,
                                                           "answer": ScriptedLLM(responses=[])})
    first = neo.ask(Q, "demo")
    assert first.status == "llm_error"                                   # the answer model failed
    neo.gateway = LLMGateway(neo.cfg, neo.runs, providers={"default": ScriptedLLM(responses=[]),
                                                           "formalize": ScriptedLLM(responses=[]),
                                                           "review": ScriptedLLM(responses=[]),
                                                           "answer": ScriptedLLM(responses=["done"])})
    second = neo.resume(first.run_id)                                    # formalize and review are not called again
    assert second.status == "proved" and second.answer == "done" and second.run_id == first.run_id


def test_replay_pins_the_snapshot(tmp_path):
    d = str(tmp_path)
    os.makedirs(f"{d}/ontology")
    copy_ontology(f"{d}/ontology/source")
    neo = Neo(make_cfg(d))
    init_demo(neo, fdic_cache=FDIC_CACHE, say=lambda *_: None)
    script(neo, branches(neo), {"verdict": "ok"})
    r = neo.ask(Q, "demo")
    neo.catalog.write_from_sql(neo.engine.connection, "deals",               # a new version with doubled branches
                               f"SELECT * REPLACE (branches_transferred * 2 AS branches_transferred) FROM read_parquet("
                               f"'{neo.catalog.path('deals', neo.catalog.snapshot().version('deals'))}')", "test")
    same = neo.replay(r.run_id)
    later = neo.replay(r.run_id, latest=True)
    assert same["same_answer"] and same["answer"] == r.proof["answer"]
    assert not later["same_answer"] and later["answer"] == 2 * r.proof["answer"]


def test_job_queue(neo, tmp_path):
    from neo.runs import RunStore
    neo.runs = RunStore(str(tmp_path / "runs"))        # an empty queue, isolated from other tests' jobs
    w = ScriptedLLM(responses=[branches(neo), {"verdict": "ok"}, branches(neo), {"verdict": "ok"}])
    neo.gateway = LLMGateway(neo.cfg, neo.runs, providers={"default": w, "formalize": w, "review": w,
                                                           "answer": ScriptedLLM(responses=["a", "b"])})
    jobs = [neo.submit(Q, "demo") for _ in range(2)]
    assert neo.work(max_jobs=2) == 2
    for j in jobs:
        job = neo.runs.job(j)
        assert job["status"] == "done" and neo.runs.run(job["run_id"])["status"] == "proved"


def test_golden_set_and_publish_gate(neo, tmp_path):
    rep = run_golden(neo, os.path.join(ROOT, "golden", "core.yaml"))
    assert rep["passed"] == rep["cases"], rep["failed"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - id: wrong_count\n    question: How many deals were failed-bank deals in the 1980s?\n"
                   "    user: demo\n    model: {form: value, interpretation: count of failed-bank deals in the 1980s,"
                   " universe: {entity: deal}, rows: 'is_assisted_failed and 1980 <= year <= 1989',"
                   " target: {expr: count()}, grounding: {constraints_applied: [{id: con_failed_bank_definition,"
                   " how: deal_category}]}}\n    expect: {status: proved, answer: 999}\n")
    before = neo.store.current_version()
    out = publish_ontology(neo, "candidate", golden=str(bad))
    assert out["published"] and not out["current"] and neo.store.current_version() == before
