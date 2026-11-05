"""Evaluation: golden question sets, and the publish gate that makes them CI for the ontology.

A golden file lists questions with the expected outcome, owned by domain experts:

  cases:
    - id: failed_deals_1980s
      question: How many failed-bank deals happened in the 1980s?
      user: admin                        # optional: the first user in config/users.yaml
      model: {...}                       # the model a correct LLM would write (used in scripted mode)
      requirements: [...]               # optional: what the requirements writer would list (scripted mode)
      expect: {status: proved, answer: 132, tolerance: 0, fidelity: matched}

Scripted mode replays each case's model through the full pipeline (retrieval, validation, compilation, proof,
certificate) without an LLM, so it checks the ontology, data and engine. Live mode asks the configured LLM, so
it also measures how well the model writer understands the ontology.

publish_ontology(): lint the source documents against the lake, publish a new version, run the golden set
against it, and make it current only if every case passes.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import yaml

from .llm.gateway import LLMGateway
from .llm.providers import ScriptedLLM
from .ontology.lint import lint


def _close(a, b, tol: float) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return all(k in a and _close(a[k], v, tol) for k, v in b.items())
    if isinstance(a, list) and isinstance(b, list):
        return len(a) >= len(b) and all(_close(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= tol * max(1.0, abs(b)) if tol else a == b
    return a == b


def run_golden(neo, path: str, live: bool = False, ontology_version: Optional[str] = None) -> Dict[str, Any]:
    spec = yaml.safe_load(open(path)) or {}
    results: List[dict] = []
    skipped: List[dict] = []
    original = neo.gateway
    try:
        for case in spec.get("cases", []):
            user = case.get("user") or neo.directory.default_user()
            if user not in neo.directory.users:      # e.g. a row-policy case for a user this deployment lacks
                skipped.append({"id": case["id"], "reason": f"user `{user}` is not in config/users.yaml"})
                continue
            if not live:
                if "model" not in case:
                    results.append({"id": case["id"], "passed": False, "reason": "no `model` for scripted mode"})
                    continue
                writer = ScriptedLLM(responses=[case["model"], {"verdict": "ok"}])
                neo.gateway = LLMGateway(neo.cfg, neo.runs, providers={
                    "default": writer, "formalize": writer, "review": writer,
                    "requirements": ScriptedLLM(responses=[{"requirements": case.get("requirements") or []}]),
                    "answer": ScriptedLLM(responses=["(scripted evaluation: no answer text)"])})
            r = neo.ask(case["question"], user, ontology_version=ontology_version)
            exp = case.get("expect", {})
            ok, why = True, []
            if exp.get("status") and r.status != exp["status"]:
                ok = False
                why.append(f"status {r.status}, expected {exp['status']}")
            if "answer" in exp and r.proof is not None and not _close(r.proof.get("answer"), exp["answer"],
                                                                        float(exp.get("tolerance", 0))):
                ok = False
                why.append(f"answer {json.dumps(r.proof.get('answer'), default=str)[:200]}, expected "
                           f"{json.dumps(exp['answer'])[:200]}")
            if exp.get("self_check") and (r.proof or {}).get("self_check", {}).get("matches") is not True:
                ok = False
                why.append("self-check did not match")
            fid = ((r.proof or {}).get("fidelity") or {}).get("status")
            if exp.get("fidelity") and not live and fid != exp["fidelity"]:
                ok = False
                why.append(f"fidelity {fid}, expected {exp['fidelity']}")
            results.append({"id": case["id"], "passed": ok, "status": r.status, "run_id": r.run_id,
                            "reason": "; ".join(why)})
    finally:
        neo.gateway = original
    passed = sum(1 for r in results if r["passed"])
    return {"file": path, "mode": "live" if live else "scripted", "cases": len(results), "passed": passed,
            "failed": [r for r in results if not r["passed"]], "skipped": skipped, "results": results}


def publish_ontology(neo, note: str = "", golden: Optional[str] = None) -> Dict[str, Any]:
    store = neo.store
    doc = store.source_document()
    snapshot = neo.catalog.snapshot()
    errors, warnings = lint(doc, neo.catalog, snapshot)
    if errors:
        return {"published": False, "errors": errors, "warnings": warnings}
    previous = store.current_version()
    version = store.publish(doc, note, make_current=golden is None)
    out = {"published": True, "version": version, "previous": previous, "warnings": warnings, "current": golden is None}
    if golden:
        report = run_golden(neo, golden, ontology_version=version)
        out["evaluation"] = {k: v for k, v in report.items() if k != "results"}
        if report["passed"] == report["cases"]:
            store.set_current(version)
            out["current"] = True
        else:
            out["current"] = False
            out["note"] = f"version {version} was published but NOT made current: the golden set failed"
    neo.audit.append("ontology_published", "system", version=version, current=out["current"], note=note)
    return out
