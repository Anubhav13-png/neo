"""Connectors bring outside data into the lake as new immutable table versions.

Everything a model can read lives in the lake and is described by the ontology, so external sources
(FDIC, market data, CRM exports) are ingested here rather than called during a question. That keeps
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


# ---------------------------------------------------------------------- synthetic enterprise data
REGIONS = [(1, "EMEA", "Europe, Middle East and Africa"), (2, "AMER", "North America"),
           (3, "APAC", "Asia Pacific"), (4, "LATAM", "Latin America")]


def ingest_synthetic(lake: LocalLakehouse, con, customers: int = 20_000, loans: int = 200_000,
                     seed: int = 7) -> Dict[str, str]:
    """Deterministic, clearly synthetic regions / customers / loans for scale, join and governance demos."""
    out = {"regions": lake.write_from_rows(con, "regions", [("region_id", "INTEGER"), ("region_code", "VARCHAR"),
                                                            ("region_name", "VARCHAR")],
                                           REGIONS, "synthetic", "4 regions")}
    h = lambda expr, salt: f"(hash(({expr}) * 1000003 + {seed * 7919 + salt}) % 1000000)"
    out["customers"] = lake.write_from_sql(con, "customers", f"""
        SELECT i AS customer_id,
               CAST({h('i', 1)} % 4 + 1 AS INTEGER) AS region_id,
               CASE CAST({h('i', 1)} % 4 AS INTEGER) WHEN 0 THEN 'EMEA' WHEN 1 THEN 'AMER'
                    WHEN 2 THEN 'APAC' ELSE 'LATAM' END AS region_code,
               CASE WHEN {h('i', 2)} < 700000 THEN 'Retail' WHEN {h('i', 2)} < 930000 THEN 'SME'
                    ELSE 'Corporate' END AS segment,
               CAST(20000 + {h('i', 3)} * 3 AS BIGINT) AS annual_income_usd,
               'Customer ' || CAST(i AS VARCHAR) AS customer_name,
               DATE '2005-01-01' + CAST({h('i', 4)} % 7000 AS INTEGER) AS onboarded_date
        FROM range(1, {customers + 1}) t(i)""", "synthetic", f"{customers} customers")
    cust_ref = f"read_parquet('{lake.path('customers', out['customers'])}')"
    out["loans"] = lake.write_from_sql(con, "loans", f"""
        WITH l AS (SELECT i AS loan_id, CAST({h('i', 11)} % {customers} + 1 AS BIGINT) AS customer_id FROM range(1, {loans + 1}) t(i))
        SELECT l.loan_id, l.customer_id, c.region_code AS booking_region,
               CASE CAST({h('l.loan_id', 12)} % 4 AS INTEGER) WHEN 0 THEN 'Mortgage' WHEN 1 THEN 'Card'
                    WHEN 2 THEN 'Auto' ELSE 'Commercial' END AS product,
               CAST(1000 + ({h('l.loan_id', 13)} * {h('l.loan_id', 14)}) % 4999000 AS BIGINT) AS balance_usd,
               CAST(150 + {h('l.loan_id', 15)} % 1500 AS INTEGER) AS rate_bp,
               CAST(({h('l.loan_id', 16)} % 150000) / 1000000.0 AS DECIMAL(9,6)) AS pd,
               CAST(0.1 + ({h('l.loan_id', 17)} % 7000) / 10000.0 AS DECIMAL(6,4)) AS lgd,
               DATE '2010-01-01' + CAST({h('l.loan_id', 18)} % 5800 AS INTEGER) AS origination_date,
               CASE WHEN {h('l.loan_id', 19)} < 940000 THEN 'current' WHEN {h('l.loan_id', 19)} < 985000
                    THEN 'delinquent' ELSE 'default' END AS status
        FROM l JOIN {cust_ref} c ON c.customer_id = l.customer_id""", "synthetic", f"{loans} loans")
    return out
