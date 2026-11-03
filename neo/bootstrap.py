"""Builds the lake and ontology from real data only: the JPMorgan deals CSV and FDIC quarterly history.

  neo init                         the deals CSV and FDIC history (downloaded once, then read from the cache)
  neo init --fdic-cache DIR        FDIC history from saved API responses in DIR (no network)
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from .config import ROOT
from .lake.connectors import ingest_csv, ingest_fdic

DEALS_CSV = os.path.join(os.path.dirname(ROOT), "jp_morgan.csv")
FDIC_SOURCES = ["industry", "holding_company:1039502", "bank:628"]


def init_demo(neo, deals_csv: Optional[str] = None, fdic_cache: Optional[str] = None, publish: bool = True,
              say=print) -> dict:
    con = neo.engine.connection
    out = {}
    path = deals_csv or DEALS_CSV
    if not os.path.exists(path):
        raise FileNotFoundError(f"the deals CSV is not at {path}; pass --deals-csv")
    out["deals"] = ingest_csv(neo.catalog, con, "deals", path)
    say(f"  ingested deals from {path}")
    out["bank_quarterly"] = ingest_fdic(neo.catalog, con, FDIC_SOURCES, cache_dir=fdic_cache)
    say("  ingested FDIC quarterly history (industry, JPMorgan's insured banks, JPMorgan Chase Bank NA)")
    deals_onto = os.path.join(neo.cfg["ontology"]["root"], "source", "deals.json")
    if not os.path.exists(deals_onto) and os.path.exists(os.path.join(ROOT, "scripts", "build_deals_ontology.py")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_deals_ontology.py")], check=False)
    if publish:
        from .evaluation import publish_ontology
        res = publish_ontology(neo, "bootstrap")
        out["ontology"] = res
        say(f"  ontology: {'published ' + res['version'] if res['published'] else 'NOT published: ' + str(res['errors'][:3])}")
    return out
