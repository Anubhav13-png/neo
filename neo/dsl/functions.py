"""The function catalogue: names, arities and the documentation shown to the LLM.

Implementations live separately in the compiler (SQL), the interpreter (exact Python) and the
prover (Z3), so the self-check compares independent implementations of the same semantics.
"""
from __future__ import annotations

from typing import Optional

# name -> (min args, max args)
AGGREGATES = {"count": (0, 1), "share": (1, 1), "sum": (1, 2), "avg": (1, 2), "min_of": (1, 2),
              "max_of": (1, 2), "any_row": (1, 1), "all_rows": (1, 1)}
SCALARS = {"implies": (2, 2), "ite": (3, 3), "abs": (1, 1), "min": (2, 2), "max": (2, 2),
           "contains": (2, 2), "startswith": (2, 2), "lower": (1, 1), "year_of": (1, 1),
           "is_null": (1, 1), "not_null": (1, 1), "coalesce": (2, 2)}
PARAM_ONLY = {"rule_param": (2, 2)}


def arity_error(name: str, n: int) -> Optional[str]:
    spec = AGGREGATES.get(name) or SCALARS.get(name) or PARAM_ONLY.get(name)
    if spec is None:
        return f"unknown function `{name}()`"
    lo, hi = spec
    if not lo <= n <= hi:
        want = str(lo) if lo == hi else f"{lo} to {hi}"
        return f"`{name}()` takes {want} argument(s), got {n}"
    return None


DOCS = """Aggregates (whole-set expressions only: parameters, targets, constraints). They range over the eligible
rows (the universe after `rows` and access policies), or over one group when `group_by` is set:
  count()  count(cond)  share(cond)  sum(x)  sum(x, cond)  avg(x)  avg(x, cond)
  min_of(x)  min_of(x, cond)  max_of(x)  max_of(x, cond)  any_row(cond)  all_rows(cond)
  Rows where x is missing are skipped and the skip count is reported in the certificate.
Logic and numbers:
  a and b, a or b, not a, implies(a, b), x if c else y, ite(c, x, y)
  + - * / // % (// and % need a positive whole-number divisor), abs(x), min(a, b), max(a, b)
  == != < <= > >=, chains like 1980 <= year <= 1989, x in ['NY', 'NJ'], x not in [...]
  Missing values: any comparison with a missing value is false (SQL three-valued logic);
  use is_null(x), not_null(x), coalesce(x, default) to handle them explicitly.
Text and dates (dates are text 'YYYY-MM-DD'; compare them as text):
  contains(s, 'Mutual') (case-insensitive), startswith(s, p), lower(s), year_of(date)
Fields: a root-entity field by its name (`balance`), a joined entity's field as `alias.field` (`cust.segment`).
Rules (parameters only): rule_param('deposit_cap', 'cap') -> 0.10 (approved rules only)
All arithmetic is exact: the compiler emits integer SQL (numerator / denominator), never floating point."""
