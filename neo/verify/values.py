"""The values an answer may cite: every proved number of a run, named, exact, with its unit.

The answer writer refers to them as slots, `{{name}}` or `{{name|billions|2}}`, and neo fills each slot from the
exact value, so a copied number cannot be wrong. The answer check uses the same catalog to verify every number
written outside a slot. Stored as JSON (exact values as fraction text) in the run's certify step.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Dict, Optional

from ..semantic.units import SCALES
from ..util import fraction_text, to_fraction

SCALE_WORDS = {"raw": 0, "": 0, "thousands": 3, "millions": 6, "billions": 9, "trillions": 12, "percent": -2}


def _entry(value, unit: Optional[str] = None, label: str = "") -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"type": "bool", "value": value, "unit": None, "label": label}
    if isinstance(value, (int, float, Fraction)):
        return {"type": "number", "value": fraction_text(to_fraction(value)), "unit": unit, "label": label}
    return {"type": "text", "value": str(value), "unit": None, "label": label}


def _unit_text(notes: dict, key: str) -> Optional[str]:
    tu = (notes or {}).get("target_units") or {}
    u = tu.get(key) or (tu.get("value") if len(tu) == 1 else None)
    return str(u) if u else None


def catalog(proof: dict, notes: Optional[dict] = None, plan_units: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    form, ans = proof.get("form"), proof.get("answer")
    exact = proof.get("exact_values") or {}
    out: Dict[str, dict] = {}

    def put(name, value, unit=None, label=""):
        if name in exact and value is not None and not isinstance(value, bool):
            value = Fraction(exact[name])
        e = _entry(value, unit, label)
        if e is not None:
            out[name] = e

    if form == "value":
        if isinstance(ans, dict):
            for k, v in ans.items():
                put(k, v, _unit_text(notes, k), f"result `{k}`")
        else:
            if "value" in exact:
                ans = Fraction(exact["value"])
            e = _entry(ans, _unit_text(notes, "value"), "the result")
            if e:
                out["answer"] = e
    elif form == "table" and isinstance(ans, list):
        out["groups"] = _entry(len(ans), "count", "number of groups in the table")
        for i, row in enumerate(ans[:25], 1):
            for k, v in row.items():
                e = _entry(v, _unit_text(notes, k) if k != "group" else None, f"row {i}, `{k}`")
                if e:
                    out[f"row{i}.{k}"] = e
    elif form == "extremum":
        rows = ans if isinstance(ans, list) else ([ans] if isinstance(ans, dict) else [])
        for i, row in enumerate(rows[:25], 1):
            for k, v in row.items():
                e = _entry(v, _unit_text(notes, "value") if k == "value" else None, f"rank {i}, `{k}`")
                if e:
                    out[f"rank{i}.{k}"] = e
    elif form == "set" and isinstance(ans, dict):
        out["count"] = _entry(ans.get("count"), "count", "number of members")
        for i, row in enumerate((ans.get("members") or [])[:15], 1):
            for k, v in row.items():
                e = _entry(v, None, f"member {i}, `{k}`")
                if e:
                    out[f"member{i}.{k}"] = e
    elif form == "check":
        for k, v in (proof.get("counts") or {}).items():
            out[f"rows_{k}"] = _entry(v, "count", f"rows {k.replace('_', ' ')}")
    elif form in ("optimisation", "feasibility"):
        if form == "optimisation" and ans not in (None, "unbounded"):
            put("objective", ans, _unit_text(notes, "objective"), "the optimum")
        for k, v in ((proof.get("solver") or {}).get("witness") or {}).items():
            e = _entry(v, None, f"value of `{k}` in the witness")
            if e:
                out[f"witness.{k}"] = e
    elif form == "plan" and isinstance(ans, dict):
        for k, v in ans.items():
            put(k, v, (plan_units or {}).get(k), f"plan result `{k}`")
    for k in ("eligible_rows", "universe_rows"):
        if isinstance(proof.get(k), int):
            out[k] = _entry(proof[k], "count", k.replace("_", " "))
    for k, v in (proof.get("parameters") or {}).items():
        e = _entry(v, None, f"parameter `{k}`")
        if e:
            out[f"param.{k}"] = e
    for k, n in (proof.get("skipped_missing") or {}).items():
        out[f"skipped.{len([x for x in out if x.startswith('skipped.')]) + 1}"] = _entry(
            n, "count", f"rows skipped in `{k}` for a missing value")
    return {k: v for k, v in out.items() if v is not None}


def exact_value(entry: dict):
    if entry["type"] == "number":
        return Fraction(entry["value"])
    return entry["value"]


_MONEY = re.compile(r"^([A-Z]{3})(?:[ _](units|thousands|millions|billions|trillions))?(?:\s*x10\^(-?[0-9]+))?$")


def unit_scale(entry: dict) -> Optional[int]:
    """Power of ten from the value's unit to whole currency units ('USD thousands' -> 3); None when not money."""
    m = _MONEY.match(str(entry.get("unit") or "").strip())
    if not m:
        return None
    return int(m.group(3)) if m.group(3) else SCALES[m.group(2) or ""]


def render_value(entry: dict, scale: str = "", decimals: Optional[int] = None) -> str:
    """Display text for a slot `{{name|scale|decimals}}`."""
    v = exact_value(entry)
    if entry["type"] == "bool":
        return "yes" if v else "no"
    if entry["type"] == "text":
        return str(v)
    k = SCALE_WORDS.get(scale, 0)
    if scale == "percent":
        shown = v * 100
    elif k:
        base = unit_scale(entry)
        shown = v * Fraction(10) ** (base or 0) / Fraction(10) ** k
    else:
        shown = v
    if decimals is None:
        decimals = 0 if shown.denominator == 1 else 2
    q = round(shown, decimals)
    text = f"{float(q):,.{decimals}f}" if decimals else f"{int(q):,}"
    return text


def llm_section(cat: Dict[str, dict]) -> str:
    """The catalog for the answer writer: names, displayed values, units (no row data beyond the certificate)."""
    L = ["### Values you can cite",
         "Write a number as a slot `{{name}}`, `{{name|scale}}` or `{{name|scale|decimals}}`; neo fills it from the "
         "exact value. scale is one of raw, thousands, millions, billions, trillions, percent (money units are "
         "converted from the value's unit; percent multiplies by 100).", "",
         "| name | value | unit | what it is |", "|---|---|---|---|"]
    for name, e in cat.items():
        shown = render_value(e, decimals=None if e["type"] != "number" or Fraction(e["value"]).denominator == 1
                             else 6)
        L.append(f"| `{name}` | {shown} | {e.get('unit') or ''} | {e.get('label', '')} |")
    return "\n".join(L)
