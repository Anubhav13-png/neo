"""The claim ledger: one row per claim a run makes, who made it, what checked it, and the result.

  R...   the question's requirements (requirements writer), checked against the model (fidelity check)
  N...   the model's results (the model writer's model, proved by the lake, Z3, the recomputation and Lean)
  A...   the written answer's claims (answer writer), checked by the answer check

The small Lean theorems the fidelity and answer checks collected (rounding, comparisons, derived values, model →
requirement) are checked here in one Lean run, cited against the library theorems that make them mean something
(Neo.Rounding, Neo.Compose, Neo.Fidelity). The ledger is appended to the certificate and drives review routing.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..proof.lean import Goals, check as lean_check

GOOD = {"matched", "proved", "true", "added", "narrower", "checked", "optimal", "feasible", "infeasible", "refuted",
        "unbounded", "holds"}
BAD = {"contradicted", "missing", "wrong", "unsupported", "rejected", "failed", "mismatch"}


def build(requirements: dict, proof: Optional[dict], answer: Optional[dict], cfg: dict) -> dict:
    rows: List[dict] = []
    goals = Goals(cfg)
    fid = (proof or {}).get("fidelity") or {}
    for r in fid.get("results") or []:
        rows.append({"id": r["id"] + (f" ({r['node']})" if r.get("node") else ""), "source": "question",
                     "text": r["text"], "checker": r.get("method", ""), "result": r["result"],
                     "detail": r.get("detail", ""), "theorem": r.get("theorem")})
    if requirements.get("status") not in ("listed",):
        rows.append({"id": "R", "source": "question", "text": "what the question requires",
                     "checker": "requirements writer", "result": "not_checked",
                     "detail": requirements.get("note") or "requirements were not listed for this run"})
    goals.items += [g for g in fid.get("lean_goals") or []]
    if fid.get("lean_goals"):
        goals.cite("fidelity")
    if proof:
        nodes = proof.get("nodes") or {}
        if nodes:
            for nid, np in nodes.items():
                rows.append({"id": nid, "source": "model", "text": np.get("interpretation", ""),
                             "checker": "exact lake aggregates · Z3 · independent recomputation",
                             "result": np.get("status"), "detail": np.get("answer_summary", "")})
        rows.append({"id": "N" if not nodes else "combine", "source": "model",
                     "text": proof.get("answer_summary", "")[:300],
                     "checker": "exact lake queries · Z3 · independent recomputation" if not nodes else
                     "exact fractions · Z3", "result": proof.get("status"),
                     "detail": f"self-check: {'matches' if (proof.get('self_check') or {}).get('matches') else 'n/a'}"})
        lean = proof.get("lean") or {}
        if lean.get("status") not in (None, "not_checked"):
            rows.append({"id": "N·Lean", "source": "model", "text": "what Z3 decided, re-checked",
                         "checker": "Lean kernel", "result": lean.get("status"),
                         "detail": f"{sum(1 for t in lean.get('theorems') or [] if t['result'] == 'checked')} of "
                                   f"{len(lean.get('theorems') or [])} theorem(s)"})
    for c in (answer or {}).get("claims") or []:
        rows.append({"id": c["id"], "source": "answer", "text": c.get("text", "")[:200],
                     "checker": c.get("checker", ""), "result": c["result"], "detail": c.get("detail", "")})
    if answer and answer.get("lean_goals"):
        goals.items += answer["lean_goals"]
        goals.cite("rounding")
    if (proof or {}).get("nodes"):
        goals.cite("combine")
    _unique(goals)
    lean = lean_check(goals, cfg, "neo claim check: requirements met by the model, and the answer's numbers") \
        if goals.items else {"status": "not_checked", "reason": "no claim theorems", "theorems": []}
    by_name = {t["name"]: t["result"] for t in lean.get("theorems") or []}
    for row in rows:
        if row.get("theorem") and row["theorem"] in by_name:
            row["lean"] = by_name[row["theorem"]]
    return {"rows": rows, "lean": lean, "summary": tally(rows), "markdown": markdown(rows, lean)}


def _unique(goals: Goals):
    seen, out = set(), []
    for g in goals.items:
        name, i = g["name"], 2
        while name in seen:
            name, i = f"{g['name']}_{i}", i + 1
        seen.add(name)
        out.append({**g, "name": name})
    goals.items = out


def tally(rows: List[dict]) -> Dict[str, int]:
    out = {"checked": 0, "failed": 0, "removed": 0, "not_checked": 0}
    for r in rows:
        res = str(r.get("result"))
        if res in GOOD:
            out["checked"] += 1
        elif res == "removed":
            out["removed"] += 1
        elif res in BAD:
            out["failed"] += 1
        else:
            out["not_checked"] += 1
    return out


def markdown(rows: List[dict], lean: dict) -> str:
    if not rows:
        return ""
    L = ["### Claim ledger", "", "| claim | from | text | checked by | result |", "|---|---|---|---|---|"]
    for r in rows:
        text = " ".join(str(r.get("text", "")).split()).replace("|", "/")[:160]
        res = str(r.get("result")) + (f" · Lean {r['lean']}" if r.get("lean") else "")
        L.append(f"| {r['id']} | {r['source']} | {text} | {r.get('checker', '')} | {res} |")
    if lean.get("status") not in (None, "not_checked"):
        th = lean.get("theorems") or []
        ok = sum(1 for t in th if t["result"] == "checked")
        L += ["", f"Lean checked {ok} of {len(th)} claim theorem(s) (rounding, comparisons, derived values, model → "
                  f"requirement): **{lean['status']}**."]
    return "\n".join(L)


def review_reasons(ledger: dict) -> List[str]:
    out = []
    for r in ledger.get("rows") or []:
        if r["source"] == "question" and r["result"] in ("contradicted", "missing"):
            out.append(f"the model does not meet requirement {r['id']}: {r.get('detail', '')[:200]}")
    removed = [r for r in ledger.get("rows") or [] if r["result"] == "removed"]
    if removed:
        out.append(f"the answer check removed {len(removed)} claim(s) the writer could not support")
    if (ledger.get("lean") or {}).get("status") == "failed":
        out.append("Lean's kernel rejected a claim theorem")
    return out
