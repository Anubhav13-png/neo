"""REST API behaviour, snapshot pinning in the lake, and ontology publishing checks."""
import json
import os
import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from neo.api import create_app
from neo.lake import DuckDBEngine, LocalLakehouse
from neo.ontology.lint import lint
from neo.ontology.store import JsonDirStore

from conftest import ROOT, script

CARDS = {"form": "value", "interpretation": "total exposure on cards, in USD", "universe": {"entity": "loan"},
         "rows": "product == 'Card'", "target": {"expr": "sum(balance_usd)"}}


def test_api_end_to_end(neo):
    script(neo, CARDS, {"verdict": "ok"}, answer="Card exposure is ...")
    c = TestClient(create_app(neo))
    assert c.get("/health").json()["ok"]
    assert c.post("/questions/sync", json={"question": "x"}).status_code == 422          # no X-User header
    assert c.post("/questions/sync", json={"question": "x"}, headers={"X-User": "mallory"}).status_code == 401
    r = c.post("/questions/sync", json={"question": "What is our total exposure on cards?"}, headers={"X-User": "demo"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "proved"
    run_id = body["run_id"]
    assert c.get(f"/runs/{run_id}", headers={"X-User": "demo"}).json()["status"] == "proved"
    assert "Proof certificate" in c.get(f"/runs/{run_id}/certificate", headers={"X-User": "demo"}).json()["certificate"]
    assert c.get(f"/runs/{run_id}", headers={"X-User": "ana"}).status_code == 404       # not ana's run
    assert c.post(f"/runs/{run_id}/feedback", json={"rating": -1, "comment": "wrong"},
                  headers={"X-User": "demo"}).json()["ok"]
    assert c.get("/reviews", headers={"X-User": "demo"}).status_code == 403
    assert c.get("/reviews", headers={"X-User": "raj"}).status_code == 200
    assert c.post(f"/runs/{run_id}/replay", json={}, headers={"X-User": "demo"}).json()["same_answer"]
    hits = c.get("/ontology/search", params={"q": "probability of default"}, headers={"X-User": "demo"}).json()
    assert hits[0]["id"] in ("term_pd", "fld_loan_pd")
    job = c.post("/questions", json={"question": "anything"}, headers={"X-User": "demo"}).json()["job_id"]
    assert c.get(f"/jobs/{job}", headers={"X-User": "demo"}).json()["status"] == "queued"


def test_web_ui_explains_every_step(neo):
    import time
    c = TestClient(create_app(neo))
    ana = {"X-User": "ana"}
    assert "question explorer" in c.get("/").text
    assert {u["id"] for u in c.get("/ui/config").json()["users"]} >= {"demo", "ana"}
    run_id = c.post("/ui/ask", json={"question": "What is our total exposure on cards?", "model": CARDS},
                    headers=ana).json()["run_id"]
    for _ in range(100):
        d = c.get(f"/ui/runs/{run_id}/explain", headers=ana).json()
        if d["finished"]:
            break
        time.sleep(0.1)
    assert d["run"]["status"] == "proved" and d["proof"]["self_check"]["matches"]
    math = " ".join(m["html"] for m in d["math"])
    assert "Σ" in math and "booking_region" in math                       # the sum, and ana's row policy
    cols = {col["name"]: col for col in d["columns"]}
    assert cols["balance_usd"]["column"] == "balance_usd" and cols["balance_usd"]["unit"] == "USD"
    assert "access policy" in cols["booking_region"]["used_in"] and d["queries"] and d["certify"]["llm"]
    assert c.get(f"/ui/runs/{run_id}/explain", headers={"X-User": "demo"}).status_code == 404
    ents = {e["id"]: e for e in c.get("/ui/ontology", headers=ana).json()["entities"]}
    assert ents["loan"]["rows"] is None and ents["loan"]["row_policy"]     # no whole-table count under a policy
    o = c.get("/ui/ontology/overview", headers=ana).json()
    assert o["counts"]["entities"] == len(o["entities"]) and o["hidden_fields"] > 0 and o["health"]
    t = c.get("/ui/ontology/trace", params={"q": "exposure on cards"}, headers=ana).json()
    assert any(f["column"] == "balance_usd" for l in t["links"] for f in l["fields"])


def test_snapshot_pinning(tmp_path):
    lake = LocalLakehouse(str(tmp_path / "lake"))
    eng = DuckDBEngine(lake)
    con = eng.connection
    lake.write_from_sql(con, "t", "SELECT 1 AS x UNION ALL SELECT 2", "test")
    pinned = eng.bind(lake.snapshot())
    lake.write_from_sql(con, "t", "SELECT 10 AS x", "test")
    assert pinned.execute(f"SELECT SUM(x) FROM {pinned.table_ref('t')}")[1][0][0] == 3      # still the old version
    now = eng.bind(lake.snapshot())
    assert now.execute(f"SELECT SUM(x) FROM {now.table_ref('t')}")[1][0][0] == 10
    assert len(lake.versions("t")) == 2 and pinned.snapshot.id != now.snapshot.id


def test_lint_checks_mappings_against_the_lake(neo):
    doc = json.loads(json.dumps(neo.store.source_document()))
    errors, _ = lint(doc, neo.catalog, neo.catalog.snapshot())
    assert not errors
    doc["fields"].append({"id": "fld_bad", "entity": "loan", "name": "ghost", "column": "no_such_column", "unit": "USD"})
    doc["fields"].append({"id": "fld_bad2", "entity": "loan", "name": "net", "column": "balance_usd", "unit": "dollars"})
    doc["relations"].append({"id": "rel_bad", "kind": "join", "from_entity": "loan", "to_entity": "customer",
                             "on": [["customer_id", "nope"]], "cardinality": "many_to_one"})
    e = " | ".join(lint(doc, neo.catalog, neo.catalog.snapshot())[0])
    assert "no_such_column" in e and "unknown unit `dollars`" in e and "join keys" in e


def test_ontology_versions_are_immutable(tmp_path):
    store = JsonDirStore(str(tmp_path / "onto"))
    v1 = store.publish({"terms": [{"id": "t1"}]}, "one")
    v2 = store.publish({"terms": [{"id": "t1"}, {"id": "t2"}]}, "two")
    assert v1 != v2 and store.current_version() == v2
    assert [x["version"] for x in store.list_versions()] == [v1, v2]
    assert store.load(v1).terms.keys() == {"t1"}
