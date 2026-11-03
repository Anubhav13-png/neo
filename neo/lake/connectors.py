"""Connectors bring outside data into the lake as new immutable table versions.

Everything a model can read lives in the lake and is described by the ontology, so external sources
(the deals CSV, FDIC history, other exports) are ingested here rather than called during a question. That keeps
every answer reproducible from a snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Sequence, Tuple

from .catalog import LocalLakehouse


def ingest_csv(lake: LocalLakehouse, con, table: str, path: str, note: str = "") -> str:
    p = os.path.abspath(path).replace("'", "''")
    return lake.write_from_sql(con, table, f"SELECT * FROM read_csv_auto('{p}', header=true)", f"csv:{path}", note)


def ingest_rows(lake: LocalLakehouse, con, table: str, columns: Sequence[Tuple[str, str]], rows, source: str,
                note: str = "") -> str:
    return lake.write_from_rows(con, table, columns, rows, source, note)


# ---------------------------------------------------------------------- FDIC BankFind
FDIC_API = "https://api.fdic.gov/banks"
FDIC_FIELDS = ["NETINCQ", "PTAXNETINCQ", "IGLSECQ", "EXTRAQ", "NIMQ", "NONIIQ", "NONIXQ", "NTLNLSQ", "ITAXQ",
               "NETINC", "ASSET", "LIAB", "EQ", "DEP", "DEPDOM", "LNLSNET", "RWAJ", "RBC"]


def _fdic_get(cache_dir: str, endpoint: str, params: dict) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    key = endpoint + "?" + urllib.parse.urlencode(sorted(params.items()))
    path = os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest() + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    url = f"{FDIC_API}/{endpoint}?" + urllib.parse.urlencode({**params, "format": "json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = json.load(r)
            json.dump(data, open(path, "w"))
            return data
        except (OSError, ValueError) as e:      # timeouts, connection resets, truncated JSON
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FDIC API failed after 4 attempts: {last}")


def fdic_history(cache_dir: str, source: str, fields: List[str], start: str = "19840101",
                 end: str = "21001231") -> Dict[str, dict]:
    """{repdte: {field: value, 'BANKS': n}} for 'industry', 'holding_company:<rssd>' or 'bank:<cert>'."""
    kind, _, ident = source.partition(":")
    rng = f"REPDTE:[{start} TO {end}]"
    if kind == "bank":
        d = _fdic_get(cache_dir, "financials", {"filters": f"CERT:{ident} AND {rng}",
                                                "fields": "REPDTE," + ",".join(fields), "limit": "1000",
                                                "sort_by": "REPDTE", "sort_order": "ASC"})
        return {x["data"]["REPDTE"]: {**{f: x["data"].get(f) for f in fields}, "BANKS": 1} for x in d.get("data", [])}
    flt = rng if kind == "industry" else f"RSSDHCR:{ident} AND {rng}"
    d = _fdic_get(cache_dir, "financials", {"filters": flt, "agg_by": "REPDTE", "agg_sum_fields": ",".join(fields),
                                            "agg_limit": "1000"})
    out = {}
    for x in d.get("data", []):
        r = x["data"]
        out[r["REPDTE"]] = {**{f: r.get(f"sum_{f}") for f in fields}, "BANKS": r.get("count")}
    return out


def ingest_fdic(lake: LocalLakehouse, con, sources: List[str], table: str = "bank_quarterly",
                fields: List[str] = None, cache_dir: str = None) -> str:
    """One row per (source, quarter). Amounts in $ thousands as the FDIC reports them."""
    fields = fields or FDIC_FIELDS
    cache = cache_dir or os.path.join(lake.root, "_cache", "fdic")
    # FDIC amounts are whole thousands of dollars: store them as integers, so exact arithmetic needs no
    # decimal scaling (a DOUBLE column is read at `double_scale` decimals, ten more digits per value)
    as_int = lambda v: None if v is None else int(round(float(v)))
    rows = []
    for src in sources:
        for repdte, rec in sorted(fdic_history(cache, src, fields).items()):
            y, m, d = int(repdte[:4]), int(repdte[4:6]), int(repdte[6:8])
            rows.append([f"{src}@{repdte}", src, f"{y:04d}-{m:02d}-{d:02d}", y, (m + 2) // 3]
                        + [as_int(rec.get(f)) for f in fields] + [rec.get("BANKS")])
    columns = [("scenario_key", "VARCHAR"), ("source", "VARCHAR"), ("quarter", "DATE"), ("year", "INTEGER"),
               ("qtr", "INTEGER")] + \
              [(f, "BIGINT") for f in fields] + [("BANKS", "BIGINT")]
    return lake.write_from_rows(con, table, columns, rows, "fdic:" + ",".join(sources),
                                f"{len(rows)} source-quarters")
