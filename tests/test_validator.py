"""The validator stops what goes wrong in enterprise analytics before any query runs."""
import pytest

from neo.semantic.validator import ValidationContext, validate

from conftest import ground


def check(neo, raw, user="demo", question="q"):
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    return validate(raw, ValidationContext(onto, question, neo.principal(user), neo.rules, neo.catalog, snap, neo.cfg))


def errors(neo, raw, **kw):
    return " | ".join(check(neo, raw, **kw).errors)


def value(target, entity="bank_quarter", **extra):
    return {"form": "value", "interpretation": "a test model, precisely stated", "universe": {"entity": entity},
            "target": {"expr": target}, **extra}


def test_valid_model_passes(neo):
    r = check(neo, value("sum(NETINCQ)"))
    assert not r.errors and r.model.notes["target_units"] == {"value": "USD thousands"}


def test_unknown_field_gets_a_hint(neo):
    assert "Did you mean: ASSET" in errors(neo, value("sum(ASSETS)"))


def test_fan_out_is_rejected_and_grain_is_required(neo):
    raw = {"form": "value", "interpretation": "branches of each buyer, through the deals it made",
           "universe": {"entity": "deal", "joins": [{"relation": "rel_deal_buyer", "as": "bought", "reverse": True}]},
           "target": {"expr": "sum(branches_transferred)"}, "grounding": ground(neo)}
    e = errors(neo, raw)
    assert "one-to-many" in e and "double count" in e
    raw["universe"]["grain"] = "bought"
    assert "double count" in errors(neo, raw)
    raw["target"] = {"expr": "sum(bought.branches_transferred)"}
    assert not check(neo, raw).errors      # summing the child side at the child grain is fine


def test_access_control(neo):
    r = check(neo, value("count(acquirer_website == 'x')", "deal", grounding=ground(neo)), user="demo")
    assert r.access_denied and "restricted" in r.access_denied[0]
    assert "confidential" in errors(neo, value("sum(failure_cost_to_fdic_000)", "deal", grounding=ground(neo)), user="demo")
    assert not check(neo, value("sum(failure_cost_to_fdic_000)", "deal", grounding=ground(neo)), user="raj").errors


def test_row_policy_attached(neo):
    r = check(neo, value("count()", "deal", grounding=ground(neo)), user="ana")
    assert r.model.policies[""].src == "(target_state == 'TX')"


def test_units(neo):
    assert "differ by a factor of 10^3" in errors(neo, value("sum(target_assets_pre_deal_000 + deal_price_usd)", "deal",
                                                             grounding=ground(neo)))
    assert "different quantities" in errors(neo, value("sum(ASSET + BANKS)"))          # dollars + a count of banks
    r = check(neo, value("sum(NIMQ + NONIIQ)"))                                        # two incomes in thousands
    assert not r.errors and "USD" in str(r.model.notes["target_units"])


def test_year_to_date_and_stock_flow(neo):
    assert "use `NETINCQ`" in errors(neo, value("sum(NETINC)"))
    assert "stock" in errors(neo, value("sum(ASSET + NETINCQ)"))


def test_rules_must_be_approved(neo):
    raw = {"form": "feasibility", "interpretation": "a test of rule approval, precisely stated",
           "universe": {"entity": "bank_quarter"}, "parameters": {"b": "rule_param('stress_capital_buffer', 'buffer')"},
           "variables": [{"name": "x", "sort": "Real"}], "constraints": [{"name": "c", "expr": "x >= b"}],
           "target": {"query": "exists"}}
    assert "not approved" in errors(neo, raw)
    raw["parameters"] = {"b": "rule_param('deposit_cap', 'cap')"}
    assert not check(neo, raw).errors


def test_aggregate_parameters_cannot_filter_rows(neo):
    raw = value("count()", parameters={"avg_assets": "avg(ASSET)"}, rows="ASSET > avg_assets")
    assert "circular" in errors(neo, raw)
    ok = value("count(big)", parameters={"avg_assets": "avg(ASSET)"}, define={"big": "ASSET > avg_assets"})
    assert not check(neo, ok).errors


def test_block_constraints_by_word_and_scope(neo):
    assert "con_failed_bank_definition" in errors(neo, value("count()", "deal"), question="How many failed banks?")
    assert "con_scenario_history" in errors(neo, value("count()"), question="How likely is a loss?")
    # the deals rule about buyers must not fire on a bank-history model just because the question says "buyer"
    assert "con_acquirer_not_jpm" not in errors(neo, value("sum(NETINCQ)"), question="Who was the buyer?")


def test_join_direction_and_aliases(neo):
    raw = {"form": "value", "interpretation": "bank history joined to deals directly, which is invalid",
           "universe": {"entity": "bank_quarter", "joins": [{"relation": "rel_deal_buyer", "as": "b"}]},
           "target": {"expr": "count()"}}
    assert "does not connect" in errors(neo, raw)


def test_strict_shapes(neo):
    assert "aggregate" in errors(neo, value("count()", rows="sum(ASSET) > 0"))
    assert "per-row value" in errors(neo, value("ASSET"))
    bad_sens = value("count()", parameters={"k": "3"}, sensitivity=[{"parameter": "k", "values": ["x"]}])
    assert "sensitivity" in errors(neo, bad_sens)


def test_cost_needs_approval(neo):
    neo.cfg["governance"]["max_rows_without_approval"] = 100
    r = check(neo, value("count()"), user="demo")
    assert r.model.needs_approval and "510" in r.model.needs_approval
    assert check(neo, value("count()"), user="raj").model.needs_approval is None   # role with large_queries
