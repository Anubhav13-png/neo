"""Column profiles: fill rate, exact text values (for enums), ranges. Computed once per table version
and cached next to it, because a version never changes."""
from __future__ import annotations

import json
import os
from typing import Dict, List

from .catalog import LocalLakehouse
from .engines import QueryEngine

ENUM_LIMIT = 30


def _is_text(t: str) -> bool:
    return t.upper().startswith(("VARCHAR", "TEXT", "STRING", "CHAR"))


def _is_number(t: str) -> bool:
    return t.upper().startswith(("BIGINT", "INT", "HUGEINT", "SMALLINT", "TINYINT", "DOUBLE", "FLOAT", "DECIMAL",
                                 "REAL", "UBIGINT", "UINTEGER", "NUMERIC"))


def profile_table(engine: QueryEngine, catalog: LocalLakehouse, table: str, version: str) -> Dict[str, dict]:
    path = catalog.profile_path(table, version)
    if os.path.exists(path):
        return json.load(open(path))
    ref = engine.table_ref(table)
    out: Dict[str, dict] = {}
    total = engine.execute(f"SELECT COUNT(*) FROM {ref}")[1][0][0]
    for col, typ in catalog.schema(table, version):
        qc = engine.dialect.q(col)
        prof = {"type": typ, "rows": total}
        filled = engine.execute(f"SELECT COUNT({qc}) FROM {ref}")[1][0][0]
        prof["fill"] = round(filled / total, 4) if total else 0
        if _is_text(typ) or typ.upper() == "BOOLEAN":
            distinct = engine.execute(f"SELECT COUNT(DISTINCT {qc}) FROM {ref}")[1][0][0]
            prof["distinct"] = distinct
            if distinct <= ENUM_LIMIT:
                vals = engine.execute(f"SELECT {qc}, COUNT(*) FROM {ref} WHERE {qc} IS NOT NULL "
                                      f"GROUP BY 1 ORDER BY 2 DESC")[1]
                prof["values"] = [[v if not isinstance(v, bool) else bool(v), n] for v, n in vals]
        elif _is_number(typ) or typ.upper() in ("DATE", "TIMESTAMP"):
            lo, hi = engine.execute(f"SELECT MIN({qc}), MAX({qc}) FROM {ref}")[1][0]
            prof["min"], prof["max"] = (str(lo) if lo is not None else None), (str(hi) if hi is not None else None)
        out[col] = prof
    json.dump(out, open(path, "w"), indent=1)
    return out


def render_profile(prof: dict, counts: bool = True) -> str:
    """counts=False for entities under a row policy: whole-table counts would reveal rows the user cannot see."""
    bits = [f"{prof['type'].lower()}"] + ([f"filled {prof['fill']:.0%}"] if counts else [])
    if "values" in prof:
        bits.append("values: " + "; ".join((f"{v!r} ({n})" if counts else repr(v)) for v, n in prof["values"][:12]))
    elif "distinct" in prof:
        bits.append(f"{prof['distinct']} distinct")
    if counts and prof.get("min") is not None:      # a max can be one hidden row's value
        bits.append(f"range {prof['min']} to {prof['max']}")
    return ", ".join(bits)
