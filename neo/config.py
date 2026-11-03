"""Loads config/neo.yaml, resolves paths against the project root, and applies environment overrides."""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, Optional

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "neo.yaml")

DEFAULTS: Dict[str, Any] = {
    "lake": {"kind": "local", "root": "data/lake"},
    "ontology": {"store": "json_dir", "root": "ontology"},
    "rules_dir": "rules",
    "retrieval": {"max_linked": 25, "hops": 1, "max_fields_per_entity": 120},
    "llm": {"default": {"provider": "scripted"}, "tasks": {}, "daily_token_budget": 2_000_000},
    "pipeline": {"max_repairs": 3, "review": True, "solver_timeout_seconds": 60},
    "verification": {"exact_max_rows": 300_000, "second_engine": "duckdb_float", "double_scale": 10},
    "proof": {"cegar_seed_rows": 256, "cegar_batch": 256, "cegar_max_iterations": 40,
              "symbolic_max_rows": 5000, "unbounded_probe": 1e15, "extremum_candidates": 64},
    "governance": {"users": "config/users.yaml", "audit_log": "var/audit.jsonl",
                   "allow_row_samples_to_llm": False, "max_rows_without_approval": 50_000_000,
                   "unit_errors": "error",
                   "review": {"high_impact_domains": ["regulatory", "board"], "min_block_constraints": 4,
                              "proved_statuses": ["proved", "optimal", "feasible", "infeasible", "refuted",
                                                  "not_expressible", "unbounded"]}},
    "runs": {"root": "var/runs"},
    "lean": {"enabled": "auto", "project": "lean", "timeout_seconds": 120, "max_row_facts": 2000, "max_core": 60},
}

_PATH_KEYS = [("lake", "root"), ("ontology", "root"), ("governance", "users"), ("governance", "audit_log"),
              ("runs", "root"), ("lean", "project")]


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def resolve(path: Optional[str], base: str = ROOT) -> Optional[str]:
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    path = path or os.environ.get("NEO_CONFIG") or DEFAULT_CONFIG
    raw = yaml.safe_load(open(path)) if os.path.exists(path) else {}
    cfg = _merge(DEFAULTS, raw or {})
    cfg = _merge(cfg, overrides or {})
    base = os.path.dirname(os.path.dirname(os.path.abspath(path))) if os.path.exists(path) else ROOT
    cfg["root"] = base
    for section, key in _PATH_KEYS:
        cfg[section][key] = resolve(cfg[section][key], base)
    cfg["rules_dir"] = resolve(cfg["rules_dir"], base)
    if os.environ.get("NEO_LLM_PROVIDER"):
        cfg["llm"]["default"] = {"provider": os.environ["NEO_LLM_PROVIDER"],
                                 "model": os.environ.get("NEO_LLM_MODEL", cfg["llm"]["default"].get("model"))}
    return cfg
