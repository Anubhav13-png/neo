"""Shared fixtures: one offline demo platform (synthetic bank history, 20,000 synthetic loans, the deals CSV),
built once per test session in a temporary directory. No network, no API key."""
import copy
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


def make_cfg(base: str, **over) -> dict:
    cfg = load_config(overrides={"lake": {"root": f"{base}/lake"}, "ontology": {"root": f"{base}/ontology"},
                                 "runs": {"root": f"{base}/runs"}, "governance": {"audit_log": f"{base}/audit.jsonl",
                                                "users": os.path.join(ROOT, "tests", "users.yaml")},
                                 "llm": {"default": {"provider": "scripted"}, "tasks": {}},
                                 "lean": {"enabled": "off"}})     # tests/test_lean.py turns it on
    for k, v in over.items():
        cfg[k].update(v)
    return cfg


@pytest.fixture(scope="session")
def base(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("neo"))
    os.makedirs(f"{d}/ontology")
    shutil.copytree(os.path.join(ROOT, "ontology", "source"), f"{d}/ontology/source")
    neo = Neo(make_cfg(d))
    init_demo(neo, loans=20_000, customers=2_000, offline=True, say=lambda *_: None)
    return d


@pytest.fixture
def neo(base):
    """A fresh platform object over the shared lake (config can be changed per test)."""
    return Neo(make_cfg(base))


def script(neo: Neo, *replies, answer: str = "scripted answer") -> ScriptedLLM:
    w = ScriptedLLM(responses=list(replies))
    neo.gateway = LLMGateway(neo.cfg, neo.runs, providers={"default": w, "formalize": w, "review": w,
                                                           "answer": ScriptedLLM(responses=[answer])})
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
