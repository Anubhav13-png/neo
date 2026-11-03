"""Unit algebra: catches adding thousands of dollars to billions, percent to ratios, levels to flows.

A unit is (dimension, scale): 'USD_thousands' is ('USD', 3), meaning value x 10^3 dollars.
Dimensionless quantities (ratios, probabilities) have dimension None; 'percent' is (None, -2) and
'bp' (basis points) is (None, -4), so comparing a percent with a ratio is a scale error.
Multiplying or dividing by a literal power of ten moves the scale: thousands / 1e6 -> billions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

SCALES = {"": 0, "units": 0, "thousands": 3, "millions": 6, "billions": 9, "trillions": 12}
NON_NUMERIC = {"text", "date", "bool", "id"}
_CUR = re.compile(r"^([A-Z]{3})(?:_(units|thousands|millions|billions|trillions))?$")


@dataclass(frozen=True)
class Unit:
    dim: Optional[str]          # None = dimensionless
    scale: int = 0
    unknown: bool = False       # stated numbers without a declared unit: compatible with anything

    def label(self) -> str:
        if self.unknown:
            return "unknown"
        if self.dim is None:
            return {0: "dimensionless", -2: "percent", -4: "basis points"}.get(self.scale, f"x10^{self.scale}")
        if self.dim in ("USD", "EUR", "GBP", "JPY", "CHF", "INR") or re.fullmatch(r"[A-Z]{3}", self.dim):
            names = {0: "", 3: " thousands", 6: " millions", 9: " billions", 12: " trillions"}
            return f"{self.dim}{names.get(self.scale, f' x10^{self.scale}')}"
        return self.dim if self.scale == 0 else f"{self.dim} x10^{self.scale}"


UNKNOWN = Unit(None, 0, unknown=True)
SCALAR = Unit(None, 0)


def parse_unit(text: Optional[str]) -> Optional[Unit]:
    """Unit for an ontology unit string; None for non-numeric fields (text, date, bool, id)."""
    if text is None or text == "":
        return UNKNOWN
    t = text.strip()
    if t in NON_NUMERIC:
        return None
    m = _CUR.match(t)
    if m:
        return Unit(m.group(1), SCALES[m.group(2) or ""])
    low = t.lower()
    if low in ("ratio", "probability", "dimensionless", "fraction", "share"):
        return SCALAR
    if low == "percent":
        return Unit(None, -2)
    if low in ("bp", "basis_points"):
        return Unit(None, -4)
    if low in ("count", "years", "days", "months", "quarters", "people", "accounts"):
        return Unit(low, 0)
    raise ValueError(f"unknown unit `{text}` (use e.g. USD, USD_thousands, ratio, percent, bp, count, years, "
                     f"text, date, bool, id)")


def _dims(dim: Optional[str]) -> dict:
    """'USD*count/years' -> {'USD': 1, 'count': 1, 'years': -1}; None -> {}."""
    out: dict = {}
    if not dim:
        return out
    num, _, den = dim.partition("/")
    for part, sign in ((num, 1), (den, -1)):
        for factor in filter(None, part.split("*")):
            base, _, exp = factor.partition("^")
            if base == "1":
                continue
            out[base] = out.get(base, 0) + sign * (int(exp) if exp else 1)
    return {k: v for k, v in out.items() if v}


def _fmt_dims(d: dict) -> Optional[str]:
    if not d:
        return None
    term = lambda k, e: k if e == 1 else f"{k}^{e}"
    num = "*".join(term(k, e) for k, e in sorted(d.items()) if e > 0)
    den = "*".join(term(k, -e) for k, e in sorted(d.items()) if e < 0)
    return (num or "1") + (f"/{den}" if den else "")


def combine(a: Optional[str], b: Optional[str], divide: bool) -> Optional[str]:
    """Dimension of a*b or a/b, simplified: USD*USD/USD -> USD."""
    da, db = _dims(a), _dims(b)
    for k, e in db.items():
        da[k] = da.get(k, 0) + (-e if divide else e)
    return _fmt_dims({k: v for k, v in da.items() if v})


def valid_unit(text: Optional[str]) -> bool:
    try:
        parse_unit(text)
        return True
    except ValueError:
        return False
