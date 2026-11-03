"""Small shared helpers: hashing, ids, exact numbers, JSON."""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
import re
import uuid
from fractions import Fraction
from typing import Any


def sha(text: str, n: int = 64) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=json_default)


def json_default(o):
    if isinstance(o, Fraction):
        return fmt_number(o)
    if isinstance(o, decimal.Decimal):
        return str(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def new_id(prefix: str) -> str:
    return f"{prefix}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def to_fraction(v) -> Fraction:
    """Exact value of a number. Floats go through their shortest repr, so 0.1 is 1/10."""
    if isinstance(v, bool):
        return Fraction(int(v))
    if isinstance(v, Fraction):
        return v
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, decimal.Decimal):
        return Fraction(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"not a finite number: {v}")
        return Fraction(repr(v))
    if isinstance(v, str):
        return Fraction(v.strip())
    raise ValueError(f"expected a number, got {v!r}")


def double_to_fraction(x: float, scale: int) -> Fraction:
    """How the compiler reads a DOUBLE column: CAST(ROUND(x * 10^scale) AS HUGEINT) / 10^scale.
    Same IEEE multiply, same round-half-away-from-zero, so both paths see the identical value."""
    v = x * (10 ** scale)
    r = math.floor(abs(v) + 0.5)
    return Fraction(int(math.copysign(r, v)) if v != 0 else 0, 10 ** scale)


def fmt_number(v) -> Any:
    """JSON-friendly display; exact fractions with a short decimal form."""
    if isinstance(v, Fraction):
        if v.denominator == 1:
            return int(v)
        return round(float(v), 6)
    return v


def fraction_text(v: Fraction) -> str:
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def is_power_of_ten(f: Fraction) -> int:
    """Exponent e if f == 10**e (e may be negative), else None."""
    if f <= 0:
        return None
    for e in range(-18, 19):
        if f == Fraction(10) ** e:
            return e
    return None


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str, n: int = 48) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:n] or "item"


def extract_json(reply: str):
    """First JSON object in an LLM reply, tolerating code fences and text around it."""
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)```", reply, flags=re.S)
    for text in fenced + [reply]:
        start = text.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:i + 1])
                            if isinstance(obj, dict):
                                return obj
                        except json.JSONDecodeError:
                            pass
                        break
            start = text.find("{", start + 1)
    return None
