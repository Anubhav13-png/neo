"""The proof certificate, rendered by code from the model and the proof (never by the LLM).

Two renderings: the full certificate for people and auditors (with the SQL that ran and row-level evidence),
and `for_llm=True`, which drops row-level evidence unless governance allows it, so the answer-writing LLM sees
only metadata and aggregates.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..llm.gateway import ROW_DATA_MARKER
from ..util import fmt_number


def _code(x) -> str:
    return "`" + " ".join(str(x).split()) + "`"


def _cell(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(fmt_number(v)) if v is not None else ""


def _table(rows: List[Dict[str, Any]], limit: int = 15, max_cols: Optional[int] = None) -> List[str]:
    if not rows:
        return []
    cols: List[str] = []
    for r in rows[:limit]:
        for k in r:
            if k not in cols:
                cols.append(k)
    cols = cols[:max_cols] if max_cols else cols
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    if len(rows) > limit:
        out.append(f"| … {len(rows) - limit} more |" + " |" * (len(cols) - 1))
    return out


def _answer_lines(proof: dict) -> List[str]:
    ans = proof.get("answer")
    if isinstance(ans, list) and ans and isinstance(ans[0], dict):
        return _table(ans, 25)
    if isinstance(ans, dict) and "members" in ans:
        head = f"{ans['count']} members" + (" (list truncated)" if ans.get("truncated") else "")
        return [head] + _table(ans["members"], 15)
    if isinstance(ans, dict):
        return _table([ans])
    return [f"**{_cell(ans)}**" if ans is not None else "(no value)"]


def render(question: str, raw: dict, proof: dict, provenance: dict, model_notes: Optional[dict] = None,
           for_llm: bool = False, allow_rows: bool = False) -> str:
    notes = model_notes or {}
    unit = "rows"
    L = ["## Proof certificate", "", f"**Question:** {question}",
         f"**Interpretation:** {raw.get('interpretation', '')}",
         f"**Form:** {proof.get('form')} · **Status:** {proof.get('status')} · **Eligible {unit}:** "
         f"{proof.get('eligible_rows', '?'):,} of {proof.get('universe_rows', '?'):,} visible to the user"
         if isinstance(proof.get('eligible_rows'), int) else
         f"**Form:** {proof.get('form')} · **Status:** {proof.get('status')}",
         "", f"**Result:** {proof.get('answer_summary', '')}", ""]
    L += _answer_lines(proof)
    if proof.get("exact_values"):
        L.append("")
        L.append("Exact values: " + ", ".join(f"{k} = {v}" for k, v in proof["exact_values"].items()))
    if proof.get("status") == "mismatch":
        L += ["", "**WARNING: the independent recomputation disagrees with the primary result. This answer is NOT "
                  "proved; it has been sent for human review.**"]

    u = raw.get("universe") or {}
    if u:
        L += ["", "### Model"]
        L.append(f"- Universe: entity `{u.get('entity')}`" + (f", scenario set `{u['scenario_set']}`" if u.get("scenario_set") else "")
                 + (f", grain `{u['grain']}`" if u.get("grain") else ""))
        for j in u.get("joins") or []:
            card = (provenance.get("joins") or {}).get(j.get("as"), "")
            L.append(f"- Join `{j.get('as')}` via `{j.get('relation')}`" + (f" ({card})" if card else ""))
        for ent, pol in (provenance.get("row_policies") or {}).items():
            L.append(f"- Access policy on `{ent}` (from the user's role): {_code(pol)}")
        L.append(f"- Rows R: {_code(raw.get('rows', 'True'))}")
        for k, v in (raw.get("define") or {}).items():
            L.append(f"- Define {k} := {_code(v)}")
        if raw.get("group_by"):
            L.append(f"- Group by: {_code(raw['group_by'])}")
        stated = set(proof.get("stated_parameters") or [])
        units = notes.get("param_units") or {}
        for k, v in (raw.get("parameters") or {}).items():
            text = v.get("expr") if isinstance(v, dict) else v
            tag = " (a stated number, not from data)" if k in stated else ""
            un = f" [{units[k]}]" if units.get(k) and units[k] != "unknown" else ""
            L.append(f"- Parameter {k} := {_code(text)} = {_cell(proof.get('parameters', {}).get(k))}{un}{tag}")
        for v in raw.get("variables") or []:
            dom = f", domain {_code(v['domain'])}" if v.get("domain") else ""
            L.append(f"- Variable {v.get('name')} ∈ {v.get('sort', 'Real')}{dom}")
        rules = provenance.get("rules") or {}
        for c in raw.get("constraints") or []:
            src = c.get("source", "")
            if src.startswith("rule:") and src[5:] in rules:
                r = rules[src[5:]]
                src = f"{src} v{r['version']} ({r['status']}, hash {r['hash']})"
            L.append(f"- Constraint [{c.get('name')}]: {_code(c.get('expr'))}" + (f" (source: {src})" if src else ""))
        for a in raw.get("assumptions") or []:
            L.append(f"- Assumption [{a.get('name')}]: {_code(a.get('expr'))}")
        tgt = {k: (v if not hasattr(v, "src") else v.src) for k, v in (raw.get("target") or {}).items()}
        L.append(f"- Target: {_code(json.dumps(tgt, default=str))}")
        tu = notes.get("target_units") or {}
        if tu:
            L.append("- Units of the result: " + ", ".join(f"{k}: {v}" for k, v in tu.items()))

    g = raw.get("grounding") or {}
    if g:
        L += ["", "### Ontology grounding"]
        for key in ("terms", "relations"):
            if g.get(key):
                L.append(f"- {key.capitalize()}: {', '.join(map(str, g[key]))}")
        for item in g.get("constraints_applied") or []:
            L.append(f"- Applied {item.get('id')}: {item.get('how')}")
        for item in g.get("constraints_not_applicable") or []:
            L.append(f"- Not applicable {item.get('id')}: {item.get('why')}")

    L += ["", "### Proof"]
    for n, step in enumerate(proof.get("verification") or [], 1):
        detail = step.get("detail")
        detail = f" — {json.dumps(detail, default=str)[:400]}" if detail not in (None, "", [], {}) else ""
        L.append(f"{n}. {step.get('step')}: {step.get('method')} → **{step.get('result')}**{detail}")
    sc = proof.get("self_check") or {}
    if sc.get("matches") is not None:
        L.append(f"- Self-check ({sc.get('method')}): {'matches ✓' if sc['matches'] else 'MISMATCH ✗'}"
                 + (f" — {sc['note']}" if sc.get("note") else ""))
        if sc.get("mismatches"):
            L += _table(sc["mismatches"], 10)
    elif sc.get("method") == "skipped":
        L.append(f"- Self-check skipped: {sc.get('note')}")
    ex = proof.get("exactness") or {}
    if ex and not ex.get("exact", True):
        L.append(f"- Approximate: these aggregates divide by a per-row value and were computed in floating point: "
                 f"{', '.join(ex.get('inexact', []))}")
    solver = proof.get("solver") or {}
    for p in solver.get("probes") or []:
        L.append(f"- Probe: drop [{p['drop']}] → {p['result']}" + (f", e.g. {p['witness']}" if p.get("witness") else ""))
    for s in proof.get("sensitivity") or []:
        L += ["", f"### Sensitivity: the whole model re-solved for each value of `{s['parameter']}`",
              f"| {s['parameter']} | status | answer |", "|---|---|---|"]
        for r in s["runs"]:
            L.append(f"| {r['value']} | {r['status']} | {_cell(r['answer'])} |")
    bt = proof.get("backtest")
    if bt:
        L += ["", "### Backtest against real events",
              f"{bt['cases']} cases checked, {len(bt['contradictions'])} contradictions, "
              f"{bt['skipped_missing_data']} skipped for missing data. "
              + ("Passed: the rules allow every event that happened." if bt["passed"] else
                 "FAILED: the rules forbid events that really happened, so the model or a rule is wrong.")]
        if allow_rows or not for_llm:
            L += _table(bt.get("contradictions") or bt.get("rows") or [], 10)

    queries = proof.get("queries") or []
    if queries:
        L += ["", f"### Lake queries ({len(queries)}, all on snapshot {provenance.get('snapshot', {}).get('id', '?')})"]
        for q in queries[:40]:
            line = f"- {q['purpose']}: {q['rows']} row(s), {q['ms']} ms"
            if not for_llm:
                line += f"\n  ```sql\n  {q['sql'][:1500]}\n  ```"
            L.append(line)

    ev = proof.get("evidence") or {}
    skipped = proof.get("skipped_missing") or {}
    show_rows = (not for_llm) or allow_rows
    if skipped or ev:
        L += ["", "### Evidence"]
        for k, n in skipped.items():
            L.append(f"- {n} eligible rows skipped in {_code(k)} because the value is missing")
        for key, label in (("counterexamples", "Counterexamples"), ("sample", "Sample of eligible rows")):
            if ev.get(key):
                if show_rows:
                    if for_llm:
                        L.append(ROW_DATA_MARKER)
                    L.append(f"- {label}:")
                    L += _table(ev[key], 10)
                else:
                    L.append(f"- {label}: {len(ev[key])} row(s), withheld from the language model by policy")
        for key in ("missing_value", "groups"):
            if key in ev:
                L.append(f"- {key.replace('_', ' ')}: {ev[key]}")

    L += ["", "### Provenance"]
    snap = provenance.get("snapshot") or {}
    L.append(f"- Data snapshot `{snap.get('id')}`: " + ", ".join(f"{t}@{v}" for t, v in (snap.get("tables") or {}).items()))
    L.append(f"- Ontology version `{provenance.get('ontology_version')}`")
    L.append(f"- Asked by {provenance.get('user')} · model writer {provenance.get('llm', '?')} · run `{provenance.get('run_id')}`")
    return "\n".join(L)
