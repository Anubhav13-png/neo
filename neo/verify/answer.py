"""The answer check: every claim in the written answer is checked by a program before anyone reads it.

The answer writer replies with JSON: the text, with numbers written as slots `{{name|scale|decimals}}` that neo
fills from the proved values (values.py), and the list of claims the text makes. Then:

  slots        filled from the exact values; a slot naming an unknown value is an error
  numbers      every number left in the text (a scan, so a claim the writer did not list is still caught) must
               be explained: a proved value at the displayed precision (units converted), a percentage that is one
               proved number's share of another, a number in the certificate, a number the question states, or a
               derived claim that checks out
  claims       number: the cited value matches the text; comparison: evaluated exactly; derived: the arithmetic
               recomputed with fractions; scope: the requirements it cites were matched by the fidelity check;
               caveat: an ontology constraint that applies; fact: unsupported unless it cites a value;
               opinion: labelled, not checkable
  caveats      every `info` constraint that applies to the question must be mentioned

Problems go back to the writer with the exact reason (a repair). What still fails after the repairs is removed (the
sentence holding it) and owed caveats are added from the ontology's own text, so the delivered answer only says what
was checked. Each checked number, comparison and derived value also becomes a small Lean theorem.
"""
from __future__ import annotations

import ast
import re
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import z3

from ..proof.lean import Goals
from ..util import extract_json
from .values import SCALE_WORDS, exact_value, render_value, unit_scale

SLOT = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*(?:\|\s*([a-z]*)\s*(?:\|\s*([0-9]+)\s*)?)?\}\}")
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(-|−)?(\$\s?)?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
                    r"(?:\s*(%|percent\b|per cent\b|thousand\b|million\b|billion\b|trillion\b|bn\b|tn\b|k\b|m\b|b\b))?"
                    r"(?![A-Za-z0-9_])")
MULT = {"thousand": 3, "k": 3, "million": 6, "m": 6, "billion": 9, "bn": 9, "b": 9, "trillion": 12, "tn": 12}
KINDS = ("number", "comparison", "derived", "scope", "caveat", "fact", "opinion")
OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, "=": lambda a, b: a == b, "!=": lambda a, b: a != b}


def parse_reply(reply: str) -> dict:
    obj = extract_json(reply or "")
    if obj and isinstance(obj.get("text"), str):
        claims = [c for c in obj.get("claims") or [] if isinstance(c, dict)]
        for i, c in enumerate(claims, 1):
            c["id"] = str(c.get("id") or f"A{i}")
            c["kind"] = str(c.get("kind", "fact")).lower() if str(c.get("kind", "")).lower() in KINDS else "fact"
        return {"text": obj["text"], "claims": claims, "format": "json"}
    return {"text": (reply or "").strip(), "claims": [], "format": "text"}


def fill(text: str, cat: Dict[str, dict]) -> Tuple[str, List[dict], List[str]]:
    """Fill slots. Returns (text, filled slots with their spans in the new text, problems)."""
    out, slots, problems, pos = [], [], [], 0
    for m in SLOT.finditer(text):
        out.append(text[pos:m.start()])
        name, scale, dec = m.group(1), m.group(2) or "", m.group(3)
        start = sum(len(x) for x in out)
        if name not in cat:
            problems.append(f"the slot `{m.group(0)}` names no proved value; use one of the names in "
                            f"'Values you can cite'")
            shown = m.group(0)
        elif scale not in SCALE_WORDS:
            problems.append(f"the slot `{m.group(0)}`: scale must be one of raw, thousands, millions, billions, "
                            f"trillions, percent")
            shown = render_value(cat[name])
        else:
            shown = render_value(cat[name], scale, int(dec) if dec else None)
        out.append(shown)
        slots.append({"name": name, "scale": scale, "decimals": int(dec) if dec else None, "shown": shown,
                      "start": start, "end": start + len(shown), "ok": name in cat and scale in SCALE_WORDS})
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), slots, problems


def numbers(text: str, skip: List[Tuple[int, int]] = ()) -> List[dict]:
    found = []
    for m in NUMBER.finditer(text):
        if any(m.start() < b and m.end() > a for a, b in skip):
            continue
        whole, frac = m.group(3).replace(",", ""), m.group(4) or ""
        word = (m.group(5) or "").lower()
        value = Fraction(whole + frac) * (-1 if m.group(1) else 1)
        found.append({"text": m.group(0).strip(), "value": value, "decimals": len(frac) - 1 if frac else 0,
                      "percent": word in ("%", "percent", "per cent"), "mult": MULT.get(word, 0),
                      "money": bool(m.group(2)), "commas": "," in m.group(3), "start": m.start(), "end": m.end(),
                      "plain_int": not frac and not word and "," not in m.group(3)})
    return found


def _tolerance(n: dict) -> Fraction:
    t = Fraction(1, 2) * Fraction(10) ** (-n["decimals"]) * Fraction(10) ** n["mult"]
    return t / 100 if n["percent"] else t


def _written(n: dict) -> Fraction:
    w = n["value"] * Fraction(10) ** n["mult"]
    return w / 100 if n["percent"] else w


def matches(n: dict, entry: dict) -> Optional[Tuple[Fraction, Fraction]]:
    """(lo, hi) bounds on the exact value, in the value's own unit, when the written number shows it honestly."""
    if entry["type"] != "number":
        return None
    v = exact_value(entry)
    w, tol = _written(n), _tolerance(n)
    base = unit_scale(entry)
    for factor in ([Fraction(1)] + ([Fraction(10) ** base] if base else [])):
        c = v * factor
        for target in (c, abs(c)) if not n["percent"] else (c, abs(c), c / 100, abs(c) / 100):
            if abs(w - target) <= tol:
                scale = factor * (Fraction(100) if n["percent"] and target in (c / 100, abs(c) / 100) else 1)
                sign = 1 if target in (c, c / 100) else (1 if c >= 0 else -1)
                lo, hi = (w - tol) / scale, (w + tol) / scale
                if sign < 0:
                    lo, hi = -hi, -lo
                return lo, hi
    return None


class _Derive:
    """Exact arithmetic over cited values: names from the catalog, numbers, + - * / and parentheses."""

    def __init__(self, cat: Dict[str, dict]):
        self.cat = cat

    def __call__(self, text: str) -> Fraction:
        tree = ast.parse(re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)",
                                lambda m: m.group(1).replace(".", "__dot__"), str(text)), mode="eval")
        return self.node(tree.body)

    def node(self, n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return Fraction(repr(n.value)) if isinstance(n.value, float) else Fraction(n.value)
        if isinstance(n, ast.Name):
            name = n.id.replace("__dot__", ".")
            e = self.cat.get(name)
            if e is None or e["type"] != "number":
                raise ValueError(f"`{name}` is not a proved number")
            return exact_value(e)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = self.node(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = self.node(n.left), self.node(n.right)
            if isinstance(n.op, ast.Div):
                if b == 0:
                    raise ValueError("division by zero")
                return a / b
            return a + b if isinstance(n.op, ast.Add) else (a - b if isinstance(n.op, ast.Sub) else a * b)
        raise ValueError(f"only + - * / over proved values are allowed, not `{ast.unparse(n)}`")


class AnswerCheck:
    def __init__(self, cat: Dict[str, dict], question: str, context_text: str, certificate: str,
                 fidelity: Optional[List[dict]], owed_caveats: List[dict], cfg: dict):
        self.cat, self.question, self.cfg = cat, question, cfg
        self.context = context_text
        self.fidelity = {r["id"]: r for r in fidelity or []}
        self.owed = owed_caveats
        self.cert_numbers = [n for n in numbers(certificate)]
        self.question_numbers = numbers(question)
        self.derive = _Derive(cat)

    # ------------------------------------------------------------------ one draft
    def check(self, parsed: dict) -> dict:
        text, slots, problems = fill(parsed["text"], self.cat)
        claims: List[dict] = []
        goals = Goals(self.cfg)
        for s in slots:
            if s["ok"]:
                claims.append({"id": f"slot:{s['name']}", "kind": "number", "text": s["shown"], "ref": s["name"],
                               "checker": "slot filled from the proved value", "result": "true",
                               "start": s["start"], "end": s["end"]})
                self._rounding(goals, f"slot_{s['name']}", s, self.cat[s["name"]])
        derived_values = []
        for c in parsed.get("claims") or []:
            res = self._claim(c, text, goals)
            claims.append(res)
            if c.get("kind") == "derived" and res["result"] == "true":
                derived_values.append(res["_value"])
        skip = [(s["start"], s["end"]) for s in slots]
        for n in numbers(text, skip):
            why = self._explain(n, derived_values, goals)
            if why is None:
                claims.append({"id": f"number@{n['start']}", "kind": "number", "text": n["text"],
                               "checker": "number scan", "result": "unsupported", "start": n["start"],
                               "end": n["end"],
                               "detail": f"`{n['text']}` is not a proved value at this precision, not in the "
                                         f"certificate and not stated in the question"})
            else:
                claims.append({"id": f"number@{n['start']}", "kind": "number", "text": n["text"],
                               "checker": "number scan", "result": "true", "detail": why, "start": n["start"],
                               "end": n["end"]})
        covered = {str(c.get("constraint")) for c in parsed.get("claims") or [] if c.get("kind") == "caveat"}
        missing = [o for o in self.owed if o["id"] not in covered] if parsed["format"] == "json" else []
        for c in claims:
            c.pop("_value", None)
            if c["result"] in ("wrong", "unsupported"):
                problems.append(f"{c['id']} \"{c['text']}\": {c.get('detail', c['result'])}")
        for o in missing:
            problems.append(f"the answer must mention {o['id']}: {o['description'][:300]} (add a sentence and list it "
                            f"as a caveat claim)")
        return {"text": text, "claims": claims, "problems": problems, "missing_caveats": missing,
                "format": parsed["format"], "lean_goals": goals.items}

    def _rounding(self, goals: Goals, name: str, slot_or_number: dict, entry: dict, bounds=None):
        if entry["type"] != "number":
            return
        v = exact_value(entry)
        if bounds is None:
            n = numbers(slot_or_number["shown"])
            if not n:
                return
            n = n[0]
            if slot_or_number.get("scale") == "percent":
                n["percent"] = True
            elif slot_or_number.get("scale"):
                n["mult"] = SCALE_WORDS[slot_or_number["scale"]]
            bounds = matches(n, entry)
        if bounds is None:
            return
        lo, hi = bounds
        goals.compute(f"rounds_{name}", f"the shown {slot_or_number.get('shown') or slot_or_number.get('text')} is "
                                        f"the exact value rounded honestly",
                      [z3.And(z3.RealVal(lo) <= z3.RealVal(v), z3.RealVal(v) <= z3.RealVal(hi))])

    def _explain(self, n: dict, derived: List[Fraction], goals: Goals) -> Optional[str]:
        for name, e in self.cat.items():
            b = matches(n, e)
            if b is not None:
                self._rounding(goals, f"n{n['start']}", n, e, b)
                return f"the proved value `{name}`"
        if n["plain_int"] and 1700 <= n["value"] <= 2100 and str(n["value"]) in self.context + self.question:
            return "a year the question or the model states"
        for d in derived:
            if abs(_written(n) - d) <= _tolerance(n):
                return "a derived value that was recomputed"
        if n["percent"]:                       # a share of one proved number in another
            nums = [(k, exact_value(e)) for k, e in list(self.cat.items())[:80] if e["type"] == "number"]
            for ka, a in nums:
                for kb, b in nums:
                    if b and ka != kb and 0 <= a / b <= 1 and abs(_written(n) - a / b) <= _tolerance(n):
                        goals.compute(f"share_n{n['start']}", f"{n['text']} is {ka} / {kb}, rounded honestly",
                                      [z3.And(z3.RealVal(_written(n) - _tolerance(n)) <= z3.RealVal(a) / z3.RealVal(b),
                                              z3.RealVal(a) / z3.RealVal(b) <= z3.RealVal(_written(n) + _tolerance(n)))])
                        return f"the share `{ka}` / `{kb}`"
        if n["mult"] == 0 and not n["percent"]:
            for c in self.cert_numbers:
                if not c["percent"] and c["mult"] == 0 and abs(n["value"] - c["value"]) <= _tolerance(n) and \
                        (n["decimals"] > 0 or n["value"] == c["value"]):
                    return "a number in the certificate"
        for q in self.question_numbers:
            if q["value"] == n["value"] and q["mult"] == n["mult"] and q["percent"] == n["percent"]:
                return "a number stated in the question"
        return None

    def _value(self, x) -> Fraction:
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return Fraction(repr(x)) if isinstance(x, float) else Fraction(x)
        if isinstance(x, str) and x in self.cat:
            return exact_value(self.cat[x])
        if isinstance(x, str):
            try:
                return self.derive(x)
            except (ValueError, SyntaxError):
                pass
            return Fraction(x.replace(",", ""))
        raise ValueError(f"`{x}` is not a proved value")

    def _claim(self, c: dict, text: str, goals: Goals) -> dict:
        kind, ctext = c.get("kind", "fact"), str(c.get("text") or "")
        res = {"id": c["id"], "kind": kind, "text": ctext}
        at = text.find(ctext) if ctext else -1
        if at >= 0:
            res.update(start=at, end=at + len(ctext))
        try:
            if kind == "number":
                ref = c.get("ref")
                if ref not in self.cat:
                    res.update(result="unsupported", checker="value catalog",
                               detail=f"cites `{ref}`, which is not a proved value")
                else:
                    ns = numbers(ctext)
                    ok = [n for n in ns if matches(n, self.cat[ref])] if ns else []
                    if ns and not ok:
                        res.update(result="wrong", checker="exact value",
                                   detail=f"the text says {ns[0]['text']}, the proved `{ref}` is "
                                          f"{render_value(self.cat[ref], decimals=6)}")
                    else:
                        res.update(result="true", checker="exact value", detail=f"`{ref}`")
            elif kind == "comparison":
                a, b, op = self._value(c.get("left")), self._value(c.get("right")), str(c.get("op", ""))
                if op not in OPS:
                    res.update(result="unsupported", checker="exact comparison", detail=f"unknown operator `{op}`")
                elif OPS[op](a, b):
                    res.update(result="true", checker="exact comparison", detail=f"{float(a):.6g} {op} {float(b):.6g}")
                    goals.compute(f"compare_{c['id']}", f"{c.get('left')} {op} {c.get('right')}",
                                  [{">": lambda x, y: x > y, "<": lambda x, y: x < y, ">=": lambda x, y: x >= y,
                                    "<=": lambda x, y: x <= y, "==": lambda x, y: x == y, "=": lambda x, y: x == y,
                                    "!=": lambda x, y: x != y}[op](z3.RealVal(a), z3.RealVal(b))])
                else:
                    res.update(result="wrong", checker="exact comparison",
                               detail=f"{c.get('left')} {op} {c.get('right')} is false ({float(a):g} vs {float(b):g})")
            elif kind == "derived":
                v = self.derive(c.get("expr", ""))
                res["_value"] = v
                shown = numbers(str(c.get("shown", "")))
                if not shown:
                    res.update(result="true", checker="exact fractions", detail=f"{c.get('expr')} = {float(v):.6g}")
                else:
                    n = shown[0]
                    w, tol = _written(n), _tolerance(n)
                    hit = next((x for x in ([v, v / 100] if n["percent"] else [v]) if abs(w - x) <= tol), None)
                    if hit is None:
                        res.update(result="wrong", checker="exact fractions",
                                   detail=f"{c.get('expr')} = {float(v):.6g}, not {n['text']}")
                    else:
                        res.update(result="true", checker="exact fractions", detail=f"{c.get('expr')} = {float(v):.6g}")
                        res["_value"] = hit
                        goals.compute(f"derived_{c['id']}", f"{c.get('expr')} rounds to {n['text']}",
                                      [z3.And(z3.RealVal(w - tol) <= z3.RealVal(hit), z3.RealVal(hit) <= z3.RealVal(w + tol))])
            elif kind == "scope":
                ids = [str(x) for x in c.get("requirements") or []]
                if not self.fidelity:
                    res.update(result="not_checkable", checker="fidelity check", detail="no requirements were checked")
                elif not ids:
                    res.update(result="not_checkable", checker="fidelity check", detail="cites no requirement")
                else:
                    bad = [i for i in ids if (self.fidelity.get(i) or {}).get("result") not in ("matched", "narrower")]
                    res.update(result="true" if not bad else "unsupported", checker="fidelity check",
                               detail=("requirements " + ", ".join(ids) + " were matched") if not bad else
                               f"requirement(s) {', '.join(bad)} were not matched")
            elif kind == "caveat":
                cid = str(c.get("constraint") or "")
                res.update(checker="ontology constraint", constraint=cid,
                           result="true" if cid else "not_checkable",
                           detail=f"states constraint `{cid}`" if cid else "names no constraint")
            elif kind == "opinion":
                res.update(result="not_checkable", checker="none", detail="an opinion or a cause: no tool can prove it")
            else:
                ref = c.get("ref")
                if ref in self.cat:
                    res.update(result="true", checker="value catalog", detail=f"`{ref}`")
                else:
                    res.update(result="unsupported", checker="certificate",
                               detail="a fact that is not in the certificate")
        except (ValueError, ZeroDivisionError, SyntaxError, TypeError) as e:
            res.update(result="unsupported", checker=res.get("checker", "exact arithmetic"), detail=str(e)[:200])
        return res


# ---------------------------------------------------------------------- the final text
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*_(\[\"'])|\n+")


def segments(text: str) -> List[Tuple[int, int]]:
    out, pos = [], 0
    for m in _SENT.finditer(text):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(text)))
    return [(a, b) for a, b in out if b > a]


def finalize(report: dict, owed_by_id: Dict[str, dict]) -> dict:
    """Remove the sentences holding claims that still fail; add owed caveats from the ontology's own text."""
    text = report["text"]
    bad = [c for c in report["claims"] if c["result"] in ("wrong", "unsupported") and "start" in c]
    drop = set()
    for c in bad:
        for i, (a, b) in enumerate(segments(text)):
            if a <= c["start"] < b:
                drop.add(i)
    segs = segments(text)
    kept = [text[a:b] for i, (a, b) in enumerate(segs) if i not in drop]
    removed = [text[a:b] for i, (a, b) in enumerate(segs) if i in drop]
    out = " ".join(s.strip() for s in kept if s.strip()) if "\n" not in text else \
        _rejoin(text, segs, drop)
    for c in report["claims"]:
        if c in bad:
            c["result"] = "removed"
    added = []
    for o in report.get("missing_caveats") or []:
        note = " ".join(o["description"].split())
        out = out.rstrip() + f"\n\n*Caveat ({o['id']}):* {note}"
        added.append(o["id"])
        report["claims"].append({"id": f"caveat:{o['id']}", "kind": "caveat", "text": note, "constraint": o["id"],
                                 "checker": "ontology constraint", "result": "added",
                                 "detail": "added by neo from the ontology's text"})
    if removed:
        out = out.rstrip() + f"\n\n*neo removed {len(removed)} sentence(s) that no check could support.*"
    return {**report, "text": out, "removed": removed, "added_caveats": added}


def _rejoin(text: str, segs, drop) -> str:
    out, pos = [], 0
    for i, (a, b) in enumerate(segs):
        out.append(text[pos:a])
        if i not in drop:
            out.append(text[a:b])
        pos = b
    out.append(text[pos:])
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", "".join(out))).strip()
