"""Human review routing: which answers a person must approve before they count.

An answer goes to the review queue when it is not a proof (failed, mismatch, undefined), when its check was
only approximate, when it touches a high-impact domain (regulatory, board reporting), when many block
constraints shaped it, when a backtest failed, or when the query needed approval for its size.
"""
from __future__ import annotations

from typing import Iterable, List, Optional


def review_reasons(cfg: dict, proof: Optional[dict], onto, raw: dict, entities: Iterable[str],
                   needs_approval: Optional[str] = None) -> List[str]:
    rc = cfg["governance"].get("review", {})
    out = []
    if needs_approval:
        out.append(f"needs approval: {needs_approval}")
    if not proof:
        return out
    status = proof.get("status")
    if status not in rc.get("proved_statuses", []):
        out.append(f"status `{status}` is not a proof")
    sc = proof.get("self_check") or {}
    if sc.get("matches") is False:
        out.append("the independent recomputation disagrees with the primary result")
    if sc.get("exact") is False:
        out.append("the independent check was approximate (second engine, universe above the exact limit)")
    if not (proof.get("exactness") or {}).get("exact", True):
        out.append("some aggregates were computed in floating point")
    if (proof.get("lean") or {}).get("status") == "failed":
        out.append("Lean's kernel rejected a fact Z3 reported")
    bt = proof.get("backtest")
    if bt and not bt.get("passed"):
        out.append("the backtest contradicts real events")
    high = set(rc.get("high_impact_domains", []))
    hit = sorted({onto.entities[e].domain for e in entities if e in onto.entities} & high)
    if hit:
        out.append(f"high-impact domain: {', '.join(hit)}")
    g = raw.get("grounding") or {}
    n = len(g.get("constraints_applied") or []) + len(g.get("constraints_not_applicable") or [])
    if n >= rc.get("min_block_constraints", 99):
        out.append(f"{n} constraints shaped this model")
    return out
