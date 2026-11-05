"""Builds the lake and ontology from real data only: the JPMorgan deals CSV (955 rows, one per bank that ended up
inside JPMorgan Chase).

  neo init                         ingest ../jp_morgan.csv and publish the ontology
  neo init --deals-csv PATH        the CSV from another place
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from .config import ROOT
from .lake.connectors import ingest_csv

DEALS_CSV = os.path.join(os.path.dirname(ROOT), "jp_morgan.csv")


def init_demo(neo, deals_csv: Optional[str] = None, publish: bool = True, say=print) -> dict:
    con = neo.engine.connection
    out = {}
    path = deals_csv or DEALS_CSV
    if not os.path.exists(path):
        raise FileNotFoundError(f"the deals CSV is not at {path}; pass --deals-csv")
    out["deals"] = ingest_csv(neo.catalog, con, "deals", path)
    say(f"  ingested deals from {path}")
    deals_onto = os.path.join(neo.cfg["ontology"]["root"], "source", "deals.json")
    if not os.path.exists(deals_onto) and os.path.exists(os.path.join(ROOT, "scripts", "build_deals_ontology.py")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_deals_ontology.py")], check=False)
    if publish:
        from .evaluation import publish_ontology
        res = publish_ontology(neo, "bootstrap")
        out["ontology"] = res
        say(f"  ontology: {'published ' + res['version'] if res['published'] else 'NOT published: ' + str(res['errors'][:3])}")
    return out
