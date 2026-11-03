"""Builds the demo lake and ontology: the JPMorgan deals CSV, FDIC quarterly history, and a synthetic loan book.

  neo init                 real FDIC data (network, cached), 200,000 synthetic loans
  neo init --offline       a clearly labelled synthetic bank-history fixture instead of FDIC (for tests / no network)
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from .config import ROOT
from .lake.connectors import ingest_csv, ingest_fdic, ingest_rows, ingest_synthetic

DEALS_CSV = os.path.join(os.path.dirname(ROOT), "jp_morgan.csv")


def ingest_bank_fixture(lake, con, table: str = "bank_quarterly") -> str:
    """Synthetic quarterly history with the FDIC table's shape, for offline tests. Not real data."""
    fields = ["NETINCQ", "PTAXNETINCQ", "IGLSECQ", "EXTRAQ", "NIMQ", "NONIIQ", "NONIXQ", "NTLNLSQ", "ITAXQ",
              "NETINC", "ASSET", "LIAB", "EQ", "DEP", "DEPDOM", "LNLSNET", "RWAJ", "RBC"]
    rows = []
    for src, base_assets, banks in (("industry", 8_000_000_000, 9000), ("holding_company:1039502", 700_000_000, 20)):
        ytd = 0
        for k in range(106):
            y, q = 2000 + k // 4, k % 4 + 1
            m, d = [(3, 31), (6, 30), (9, 30), (12, 31)][q - 1]
            assets = base_assets + base_assets * k // 40
            ptax = assets // 400 + (k * 7919 % 97) * assets // 200_000 - (assets // 150 if k in (35, 36) else 0)
            gains = (k % 5 - 2) * assets // 100_000
            tax = max(0, (ptax + gains) // 4)
            net = ptax + gains - tax
            ytd = net if q == 1 else ytd + net
            vals = {"NETINCQ": net, "PTAXNETINCQ": ptax, "IGLSECQ": gains, "EXTRAQ": 0, "NIMQ": assets // 120,
                    "NONIIQ": assets // 250, "NONIXQ": assets // 160, "NTLNLSQ": assets // 2000, "ITAXQ": tax,
                    "NETINC": ytd, "ASSET": assets, "LIAB": assets * 9 // 10, "EQ": assets // 10,
                    "DEP": assets * 7 // 10, "DEPDOM": assets * 6 // 10 if src == "industry" else assets * 7 // 100,
                    "LNLSNET": assets // 2, "RWAJ": assets * 6 // 10, "RBC": assets * 8 // 100}
            rep = f"{y:04d}{m:02d}{d:02d}"
            rows.append([f"{src}@{rep}", src, f"{y:04d}-{m:02d}-{d:02d}", y, q] + [vals[f] for f in fields]
                        + [banks - k * 20 if src == "industry" else banks])
    columns = [("scenario_key", "VARCHAR"), ("source", "VARCHAR"), ("quarter", "DATE"), ("year", "INTEGER"),
               ("qtr", "INTEGER")] + [(f, "BIGINT") for f in fields] + [("BANKS", "BIGINT")]
    return ingest_rows(lake, con, table, columns, rows, "fixture:synthetic", "synthetic bank history (not FDIC data)")


def init_demo(neo, loans: int = 200_000, customers: int = 20_000, offline: bool = False,
              deals_csv: Optional[str] = None, publish: bool = True, say=print) -> dict:
    con = neo.engine.connection
    out = {}
    path = deals_csv or DEALS_CSV
    if os.path.exists(path):
        out["deals"] = ingest_csv(neo.catalog, con, "deals", path)
        say(f"  ingested deals from {path}")
    else:
        say(f"  skipped deals: {path} not found")
    out.update(ingest_synthetic(neo.catalog, con, customers=customers, loans=loans))
    say(f"  ingested synthetic regions / {customers:,} customers / {loans:,} loans")
    if offline:
        out["bank_quarterly"] = ingest_bank_fixture(neo.catalog, con)
        say("  ingested a SYNTHETIC bank-history fixture (offline mode)")
    else:
        out["bank_quarterly"] = ingest_fdic(neo.catalog, con, ["industry", "holding_company:1039502", "bank:628"])
        say("  ingested FDIC quarterly history (industry, JPMorgan's insured banks, JPMorgan Chase Bank NA)")
    deals_onto = os.path.join(neo.cfg["ontology"]["root"], "source", "deals.json")
    if not os.path.exists(deals_onto) and os.path.exists(os.path.join(ROOT, "scripts", "build_deals_ontology.py")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_deals_ontology.py")], check=False)
    if publish:
        from .evaluation import publish_ontology
        res = publish_ontology(neo, "demo bootstrap")
        out["ontology"] = res
        say(f"  ontology: {'published ' + res['version'] if res['published'] else 'NOT published: ' + str(res['errors'][:3])}")
    return out
