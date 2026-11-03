"""Rule Registry: legal, regulatory and business rules with versions, owners and an approval status.

Only `approved` rules that are in force can be cited by a model (`source: rule:<id>`) or read with
rule_param(); drafts are visible to authors but rejected by the validator. Each question pins the
version and content hash of every rule, so a certificate names the exact rule text that was applied.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
from typing import Dict, List, Optional

import yaml

from .util import sha, stable_json


class RuleError(ValueError):
    pass


class RuleRegistry:
    def __init__(self, rules_dir: str):
        self.dir = rules_dir
        self.rules: Dict[str, dict] = {}
        for path in sorted(glob.glob(os.path.join(rules_dir, "*.yaml"))):
            if os.path.basename(path).startswith("_"):
                continue
            rule = yaml.safe_load(open(path)) or {}
            if not rule.get("id"):
                continue
            rule.setdefault("version", 1)
            rule.setdefault("status", "draft")
            rule["hash"] = sha(stable_json({k: v for k, v in rule.items() if k != "hash"}), 16)
            self.rules[rule["id"]] = rule

    def all(self) -> List[dict]:
        return list(self.rules.values())

    def get(self, rule_id: str) -> dict:
        if rule_id not in self.rules:
            raise RuleError(f"no rule `{rule_id}` in the registry")
        return self.rules[rule_id]

    def check_citable(self, rule_id: str, on: Optional[str] = None) -> Optional[str]:
        """None if the rule may be used; otherwise why not."""
        if rule_id not in self.rules:
            return f"no rule `{rule_id}` in the registry"
        r = self.rules[rule_id]
        if r["status"] != "approved":
            return f"rule `{rule_id}` is `{r['status']}`, not approved; only approved rules can be used"
        today = on or dt.date.today().isoformat()
        if r.get("effective_to") and str(r["effective_to"]) < today:
            return f"rule `{rule_id}` stopped applying on {r['effective_to']}"
        return None

    def param(self, rule_id: str, name: str):
        problem = self.check_citable(rule_id)
        if problem:
            raise RuleError(problem)
        params = self.rules[rule_id].get("parameters") or {}
        if name not in params:
            raise RuleError(f"rule `{rule_id}` has no parameter `{name}` (has: {', '.join(params)})")
        return params[name]["value"], params[name].get("unit")

    def pinned(self) -> Dict[str, dict]:
        return {i: {"version": r["version"], "status": r["status"], "hash": r["hash"]} for i, r in self.rules.items()}
