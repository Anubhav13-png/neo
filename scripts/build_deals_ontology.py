"""Builds ontology/source/deals.json from the existing ontology (a version in ../.evoontology/versions, by default
the active one, plus ../deposit_cap_model/ontology_additions.json) and the physical schema of jp_morgan.csv.

This is the pattern for connecting an existing ontology store: keep its terms, relations and constraints,
and add what neo needs that a conceptual ontology usually lacks: the physical column of every field, its
unit, its time semantics, its classification, and join relations with cardinality.

  .venv/bin/python scripts/build_deals_ontology.py                     # the active evoontology version
  .venv/bin/python scripts/build_deals_ontology.py --version cand_r2   # another version
  .venv/bin/python scripts/build_deals_ontology.py --ontology /path/to/.evoontology            # another project
  .venv/bin/python scripts/build_deals_ontology.py --ontology /path/to/.evoontology/versions/v3  # one version folder
  .venv/bin/neo ontology publish --eval golden/core.yaml               # then publish it behind the golden set

--ontology takes an evoontology project folder (with versions/ and active.json) or one version folder (with
terms.json, mappings.json, relations.json, constraints.json, evidence.json). --additions "" skips the additions.
"""
import argparse
import json
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JP = os.path.dirname(HERE)
EVO = os.path.join(JP, ".evoontology")
ADDITIONS = os.path.join(JP, "deposit_cap_model", "ontology_additions.json")
CSV = os.path.join(JP, "jp_morgan.csv")
OUT = os.path.join(HERE, "ontology", "source", "deals.json")


def items(path):
    d = json.load(open(path))
    return d if isinstance(d, list) else next(v for v in d.values() if isinstance(v, list))


def unit_for(col, sql_type):
    t = sql_type.upper()
    if t.startswith("VARCHAR"):
        return "text"
    if t.startswith("DATE") or t.startswith("TIMESTAMP"):
        return "date"
    if t.startswith("BOOL"):
        return "bool"
    if col.endswith("_000"):
        return "USD_thousands"
    if col == "deal_price_usd":
        return "USD"
    if col == "year":
        return "years"
    if col.endswith(("_cert", "_rssd")) or col in ("change_code", "transaction_number"):
        return "id"
    if col in ("generation", "branches_transferred"):
        return "count"
    if col.endswith("_ratio"):
        return "ratio"
    return None


FILES = ("terms.json", "mappings.json", "relations.json", "constraints.json", "evidence.json")


def active_version(evo: str) -> str:
    path = os.path.join(evo, "active.json")
    return json.load(open(path))["active_version"] if os.path.exists(path) else "ontology_v0"


def version_folder(ontology: str, version) -> tuple:
    """(folder holding the five files, version name) from a project folder or a version folder."""
    ontology = os.path.abspath(os.path.expanduser(ontology))
    if not os.path.isdir(ontology):
        hint = f" (did you mean ~{ontology}?)" if ontology.startswith("/Desktop") else ""
        sys.exit(f"no such folder: {ontology}{hint}")
    if os.path.basename(ontology) == "versions" and not os.path.exists(os.path.join(ontology, "terms.json")):
        ontology = os.path.dirname(ontology)          # the versions/ folder itself: use its project
    if os.path.exists(os.path.join(ontology, "terms.json")):
        if version:
            sys.exit("--version applies to a project folder; --ontology already points at one version folder")
        return ontology, os.path.basename(ontology)
    version = version or active_version(ontology)
    return os.path.join(ontology, "versions", version), version


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ontology", default=EVO, help="evoontology project folder or one version folder "
                                                    f"(default: {EVO})")
    ap.add_argument("--version", default=None, help="version inside the project folder (default: the active one)")
    ap.add_argument("--additions", default=ADDITIONS, help='extra terms/relations/constraints/evidence ("" for none)')
    ap.add_argument("--csv", default=CSV, help="the data file whose columns become fields")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    V0, version = version_folder(args.ontology, args.version)
    missing = [f for f in FILES if not os.path.exists(os.path.join(V0, f))]
    if not os.path.isdir(V0) or missing:
        sys.exit(f"existing ontology not found at {V0}" + (f" (missing {', '.join(missing)})" if missing else ""))
    if not os.path.exists(args.csv):
        sys.exit(f"data file not found: {args.csv}")
    schema = duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_csv_auto(?, header=true)", [args.csv]).fetchall()
    v0_terms, v0_maps = items(os.path.join(V0, "terms.json")), items(os.path.join(V0, "mappings.json"))
    v0_rels, v0_cons = items(os.path.join(V0, "relations.json")), items(os.path.join(V0, "constraints.json"))
    v0_ev = items(os.path.join(V0, "evidence.json"))
    add = json.load(open(args.additions)) if args.additions and os.path.exists(args.additions) else {}
    print(f"ontology: {V0}\nadditions: {args.additions if add else 'none'}\ndata: {args.csv}")
    term_of = {}
    for m in v0_maps:
        term_of.setdefault(m["column"], m["term_id"])
    doc = {"note": f"Generated by scripts/build_deals_ontology.py from {version} + the jp_morgan.csv schema.",
           "terms": [], "entities": [], "fields": [], "relations": [], "constraints": [], "evidence": []}
    seen_terms = set()
    for t in v0_terms + add.get("terms", []):
        if t["id"] in seen_terms:
            continue
        seen_terms.add(t["id"])
        doc["terms"].append({"id": t["id"], "name": t.get("name", ""), "kind": t.get("type", "concept"),
                             "definition": t.get("definition", ""), "aliases": t.get("aliases", []),
                             "domain": "m&a", "evidence": t.get("evidence", [])})
    doc["entities"].append({"id": "deal", "name": "Deal (bank that ended up in JPMorgan Chase)", "table": "deals",
                            "key": "target_cert", "grain": "target bank (one row per bank absorbed into the tree)",
                            "time_field": "effective_date", "label": "target_name", "domain": "m&a",
                            "classification": "public", "owner": "corporate-development",
                            "definition": "955 banks that ended up inside JPMorgan Chase, 1970-2023 (FDIC BankFind).",
                            "aliases": ["deals", "acquisitions", "mergers", "banks acquired", "transactions"]})
    for col, typ, *_ in schema:
        f = {"id": f"fld_deal_{col}", "entity": "deal", "name": col, "column": col, "classification": "public"}
        u = unit_for(col, typ)
        if u:
            f["unit"] = u
        if col in term_of and term_of[col] in seen_terms:
            f["term"] = term_of[col]
        if col.endswith("_pre_deal_000") or col.startswith("target_last_reported") or col.endswith("current_assets_000"):
            f["time_semantics"] = "stock"
        doc["fields"].append(f)
    doc["fields"] += [
        {"id": "fld_deal_is_assisted_failed", "entity": "deal", "name": "is_assisted_failed", "term": "term_assisted_failed_deal",
         "expression": "deal_category == 'Assisted / failed-bank deal'", "dtype": "bool", "unit": "bool",
         "classification": "public", "description": "failed-bank or FDIC-assisted deal"},
        {"id": "fld_deal_is_direct", "entity": "deal", "name": "is_direct", "term": "term_direct_acquisition",
         "expression": "generation == 1", "dtype": "bool", "unit": "bool", "classification": "public",
         "description": "bought directly by JPMorgan's lead bank (cert 628)"}]
    for f in doc["fields"][-2:]:                  # another ontology may not have these two terms
        if f["term"] not in seen_terms:
            del f["term"]
    doc["relations"].append({"id": "rel_deal_buyer", "kind": "join", "from_entity": "deal", "to_entity": "deal",
                             "on": [["acquirer_cert", "target_cert"]], "cardinality": "many_to_one", "alias": "buyer",
                             "description": "the buyer's own row in the tree (acquirer_cert = target_cert of another row)"})
    for r in v0_rels + add.get("relations", []):
        doc["relations"].append({"id": r["id"], "kind": "semantic", "source": r.get("source"), "target": r.get("target"),
                                 "description": " ".join(filter(None, [r.get("relation_type"), r.get("connection_condition"),
                                                                       r.get("description")]))})
    for c in v0_cons + add.get("constraints", []):
        target = c.get("target") if c.get("target") in seen_terms else "deal"
        doc["constraints"].append({"id": c["id"], "kind": c.get("constraint_type", "data_quality"),
                                   "severity": "block" if c.get("severity") == "block" else "info", "target": target,
                                   "triggers": c.get("trigger_keywords", []), "description": c.get("description", ""),
                                   "formula": c.get("formula")})
    for e in v0_ev + add.get("evidence", []):
        doc["evidence"].append({"id": e["id"], "source": e.get("source", ""), "query": e.get("query", ""),
                                "result": str(e.get("result", "")), "timestamp": e.get("timestamp", "")})
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w"), indent=1)
    print(f"wrote {args.out} from {version}: {', '.join(f'{len(v)} {k}' for k, v in doc.items() if isinstance(v, list))}")


if __name__ == "__main__":
    main()
