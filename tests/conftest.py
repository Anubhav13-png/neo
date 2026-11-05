"""Shared fixtures: one platform over the real data (the deals CSV, 955 rows), built once per test session in a
temporary directory. No network, no API key, no synthetic data."""
import copy
import json
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from neo.bootstrap import init_demo  # noqa: E402
from neo.config import load_config  # noqa: E402
from neo.llm.gateway import LLMGateway  # noqa: E402
from neo.llm.providers import ScriptedLLM  # noqa: E402
from neo.orchestrator import Neo  # noqa: E402


# The real data is all public; access tests need fields above a user's clearance, so the tests' copy of the
# ontology raises two deals fields (the ontology in ontology/source is unchanged).
TEST_CLASSIFICATION = {"failure_cost_to_fdic_000": "confidential", "acquirer_website": "restricted"}


def copy_ontology(dest: str):
    shutil.copytree(os.path.join(ROOT, "ontology", "source"), dest)
    path = os.path.join(dest, "deals.json")
    doc = json.load(open(path))
    for f in doc["fields"]:
        if f["name"] in TEST_CLASSIFICATION:
            f["classification"] = TEST_CLASSIFICATION[f["name"]]
    json.dump(doc, open(path, "w"), indent=1)


def ground(neo) -> dict:
    """Grounding that marks every block constraint not applicable: for tests about something else."""
    onto = neo.ontology.load(None, neo.catalog.snapshot())
    return {"constraints_not_applicable": [{"id": c.id, "why": "not what this test is about"}
                                           for c in onto.constraints.values() if c.severity == "block"]}


def make_cfg(base: str, **over) -> dict:
    cfg = load_config(overrides={"lake": {"root": f"{base}/lake"}, "ontology": {"root": f"{base}/ontology"},
                                 "runs": {"root": f"{base}/runs"}, "governance": {"audit_log": f"{base}/audit.jsonl",
                                                "users": os.path.join(ROOT, "tests", "users.yaml")},
                                 "llm": {"default": {"provider": "scripted"}, "tasks": {}},
                                 "lean": {"enabled": "off"}})     # tests/test_lean.py turns it on
    cfg["llm"]["tasks"] = {}       # only the providers a test passes; no requirements writer unless it scripts one
    for k, v in over.items():
        cfg[k].update(v)
    return cfg


@pytest.fixture(scope="session")
def base(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("neo"))
    os.makedirs(f"{d}/ontology")
    copy_ontology(f"{d}/ontology/source")
    neo = Neo(make_cfg(d))
    init_demo(neo, say=lambda *_: None)
    return d


@pytest.fixture
def neo(base):
    """A fresh platform object over the shared lake (config can be changed per test)."""
    return Neo(make_cfg(base))


def script(neo: Neo, *replies, answer="scripted answer", requirements=None) -> ScriptedLLM:
    """Scripted model writer and reviewer (replies in order), answer writer (one reply, or a list for repairs) and,
    when `requirements` is given, the requirements writer."""
    w = ScriptedLLM(responses=list(replies))
    providers = {"default": w, "formalize": w, "review": w,
                 "answer": ScriptedLLM(responses=list(answer) if isinstance(answer, list) else [answer])}
    if requirements is not None:
        providers["requirements"] = ScriptedLLM(responses=[{"requirements": requirements}])
    neo.gateway = LLMGateway(neo.cfg, neo.runs, providers=providers)
    return w


@pytest.fixture
def scripted():
    return script


def table_path(neo: Neo, table: str) -> str:
    snap = neo.catalog.snapshot()
    return neo.catalog.path(table, snap.version(table))


def direct(neo: Neo, sql: str):
    """Independent ground truth: hand-written SQL over the Parquet files, not through neo's compiler."""
    import duckdb
    for t in neo.catalog.tables():
        sql = sql.replace("{" + t + "}", f"read_parquet('{table_path(neo, t)}')")
    return duckdb.connect().execute(sql).fetchall()
