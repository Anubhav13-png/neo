"""Who is asking, and what may they see.

config/users.yaml defines roles (clearance, row policies, permissions) and users (roles + attributes).
In production the API gateway authenticates (OIDC / SAML) and this directory maps the identity's groups
to roles; the lake additionally enforces its own row and column security under the user's identity.

Row policies are expressions in the model language over an entity's fields, with {attribute}
placeholders filled from the user's attributes as quoted literals, e.g.
  regional_analyst: row_policies: {loan: "booking_region == {region}"}
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

from ..dsl import ExprError, parse
from ..ontology.model import CLEARANCE


class AccessError(PermissionError):
    pass


@dataclass
class Principal:
    id: str
    name: str
    roles: List[str]
    clearance: str
    row_policies: Dict[str, str] = field(default_factory=dict)   # entity -> filter expression
    can_approve: bool = False
    large_queries: bool = False
    daily_token_budget: int = 2_000_000
    attributes: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "roles": self.roles, "clearance": self.clearance,
                "row_policies": self.row_policies, "can_approve": self.can_approve}


def _literal(v) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


class Directory:
    def __init__(self, path: str):
        data = yaml.safe_load(open(path)) if path and os.path.exists(path) else {}
        self.roles = data.get("roles") or {}
        self.users = data.get("users") or {}

    def default_user(self) -> str:
        """The first configured user: who the CLI and the golden set act as when no user is named."""
        if not self.users:
            raise AccessError("no users are configured (config/users.yaml)")
        return next(iter(self.users))

    def principal(self, user_id: str) -> Principal:
        if user_id not in self.users:
            raise AccessError(f"unknown user `{user_id}`")
        u = self.users[user_id]
        attrs = u.get("attributes") or {}
        roles = u.get("roles") or []
        clearance, policies = "public", {}
        can_approve = large = False
        budget = 0
        for rname in roles:
            role = self.roles.get(rname)
            if role is None:
                raise AccessError(f"user `{user_id}` has unknown role `{rname}`")
            c = role.get("clearance", "public")
            if CLEARANCE.index(c) > CLEARANCE.index(clearance):
                clearance = c
            can_approve |= bool(role.get("approve_reviews"))
            large |= bool(role.get("large_queries"))
            budget = max(budget, int(role.get("daily_token_budget", 2_000_000)))
            for ent, expr in (role.get("row_policies") or {}).items():
                try:
                    text = expr.format(**{k: _literal(v) for k, v in attrs.items()})
                except KeyError as e:
                    raise AccessError(f"role `{rname}` needs user attribute {e} for its row policy on `{ent}`")
                parse(text)
                policies.setdefault(ent, []).append(text)
        unrestricted = {ent for rname in roles for ent in (self.roles[rname].get("unrestricted") or [])}
        combined = {ent: " or ".join(f"({p})" for p in ps) for ent, ps in policies.items() if ent not in unrestricted}
        return Principal(user_id, u.get("name", user_id), roles, clearance, combined, can_approve, large,
                         budget or 2_000_000, {k: str(v) for k, v in attrs.items()})
