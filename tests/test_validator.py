"""The validator stops what goes wrong in enterprise analytics before any query runs."""
import pytest

from neo.semantic.validator import ValidationContext, validate


def check(neo, raw, user="demo", question="q"):
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    return validate(raw, ValidationContext(onto, question, neo.principal(user), neo.rules, neo.catalog, snap, neo.cfg))


def errors(neo, raw, **kw):
    return " | ".join(check(neo, raw, **kw).errors)


def value(target, entity="loan", **extra):
    return {"form": "value", "interpretation": "a test model, precisely stated", "universe": {"entity": entity},
            "target": {"expr": target}, **extra}


def test_valid_model_passes(neo):
    r = check(neo, value("sum(balance_usd)"))
    assert not r.errors and r.model.notes["target_units"] == {"value": "USD"}


def test_unknown_field_gets_a_hint(neo):
    assert "Did you mean: balance_usd" in errors(neo, value("sum(balance)"))


def test_fan_out_is_rejected_and_grain_is_required(neo):
    raw = {"form": "value", "interpretation": "income of customers through their loans",
           "universe": {"entity": "customer", "joins": [{"relation": "rel_loan_customer", "as": "ln"}]},
           "target": {"expr": "sum(annual_income_usd)"}}
    e = errors(neo, raw, user="raj")
    assert "one-to-many" in e and "double count" in e
    raw["universe"]["grain"] = "ln"
    assert "double count" in errors(neo, raw, user="raj")
    raw["target"] = {"expr": "sum(ln.balance_usd)"}
    assert not check(neo, raw, user="raj").errors      # summing the child side at the child grain is fine


def test_access_control(neo):
    r = check(neo, value("count(customer_name == 'x')", entity="customer"), user="demo")
    assert r.access_denied and "restricted" in r.access_denied[0]
    assert "confidential" in errors(neo, value("sum(annual_income_usd)", entity="customer"), user="demo")
    assert not check(neo, value("sum(annual_income_usd)", entity="customer"), user="raj").errors


def test_row_policy_attached(neo):
    r = check(neo, value("count()"), user="ana")
    assert r.model.policies[""].src == "(booking_region == 'EMEA')"


def test_units(neo):
    assert "differ by a factor of 10^3" in errors(neo, value("sum(target_assets_pre_deal_000 + deal_price_usd)", "deal",
                                                         grounding={"constraints_not_applicable": [
                                                             {"id": "con_unit_thousands", "why": "t"},
                                                             {"id": "con_price_unreliable", "why": "t"},
                                                             {"id": "con_predeal_coverage", "why": "t"}]}))
    assert "different quantities" in errors(neo, value("sum(balance_usd + rate_bp)"))   # dollars + basis points
    assert not check(neo, value("sum(balance_usd * rate_bp / 10000)")).errors           # interest in dollars
    assert "USD" in str(check(neo, value("sum(balance_usd * rate)")).model.notes["target_units"])


def test_year_to_date_and_stock_flow(neo):
    assert "use `NETINCQ`" in errors(neo, value("sum(NETINC)", "bank_quarter"))
    assert "stock" in errors(neo, value("sum(ASSET + NETINCQ)", "bank_quarter"))


def test_rules_must_be_approved(neo):
    raw = {"form": "feasibility", "interpretation": "a test of rule approval, precisely stated",
           "universe": {"entity": "loan"}, "parameters": {"b": "rule_param('stress_capital_buffer', 'buffer')"},
           "variables": [{"name": "x", "sort": "Real"}], "constraints": [{"name": "c", "expr": "x >= b"}],
           "target": {"query": "exists"}}
    assert "not approved" in errors(neo, raw)
    raw["parameters"] = {"b": "rule_param('deposit_cap', 'cap')"}
    assert not check(neo, raw).errors


def test_aggregate_parameters_cannot_filter_rows(neo):
    raw = value("count()", parameters={"avg_bal": "avg(balance_usd)"}, rows="balance_usd > avg_bal")
    assert "circular" in errors(neo, raw)
    ok = value("count(big)", parameters={"avg_bal": "avg(balance_usd)"}, define={"big": "balance_usd > avg_bal"})
    assert not check(neo, ok).errors


def test_block_constraints_by_word_and_scope(neo):
    e = errors(neo, value("sum(expected_loss_usd)"), question="What is our expected loss?")
    assert "con_expected_loss_meaning" in e
    # the deals-only price rule must not fire on a loan model just because the question says "cost"
    assert "con_price_unreliable" not in errors(neo, value("sum(balance_usd)"), question="What does this cost?")


def test_join_direction_and_aliases(neo):
    raw = {"form": "value", "interpretation": "loans joined to a region directly, which is invalid",
           "universe": {"entity": "loan", "joins": [{"relation": "rel_customer_region", "as": "r"}]},
           "target": {"expr": "count()"}}
    assert "does not connect" in errors(neo, raw)


def test_strict_shapes(neo):
    assert "aggregate" in errors(neo, value("count()", rows="sum(balance_usd) > 0"))
    assert "per-row value" in errors(neo, value("balance_usd"))
    bad_sens = value("count()", parameters={"k": "3"}, sensitivity=[{"parameter": "k", "values": ["x"]}])
    assert "sensitivity" in errors(neo, bad_sens)


def test_cost_needs_approval(neo):
    neo.cfg["governance"]["max_rows_without_approval"] = 1000
    r = check(neo, value("count()"), user="demo")
    assert r.model.needs_approval and "20,000" in r.model.needs_approval
    assert check(neo, value("count()"), user="raj").model.needs_approval is None   # role with large_queries
