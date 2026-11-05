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


def value(target, entity="deal", **extra):
    return {"form": "value", "interpretation": "a test model, precisely stated", "universe": {"entity": entity},
            "target": {"expr": target}, **extra}


def test_valid_model_passes(neo):
    r = check(neo, value("sum(target_assets_pre_deal_000)", grounding=ground(neo)))
    assert not r.errors and r.model.notes["target_units"] == {"value": "USD thousands"}


def test_unknown_field_gets_a_hint(neo):
    assert "Did you mean: target_assets_pre_deal_000" in errors(neo, value("sum(target_asset_pre_deal_000)"))


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
    assert "different quantities" in errors(neo, value("sum(target_assets_pre_deal_000 + branches_transferred)",
                                                       grounding=ground(neo)))          # dollars + a count of branches
    r = check(neo, value("sum(target_assets_pre_deal_000 + target_deposits_pre_deal_000)", grounding=ground(neo)))
    assert not r.errors and "USD" in str(r.model.notes["target_units"])


def test_year_to_date_and_stock_flow(neo):
    """The deals ontology has no flows or year-to-date fields, so the rule is tested on a copy where two fields
    are declared that way (only the declaration changes; the data is the real file)."""
    snap = neo.catalog.snapshot()
    onto = neo.ontology.load(None, snap)
    cost = onto.field("deal", "failure_cost_to_fdic_000")
    branches = onto.field("deal", "branches_transferred")
    saved = (cost.time_semantics, branches.time_semantics, branches.per_period)
    cost.time_semantics = "flow"
    branches.time_semantics, branches.per_period = "ytd", "generation"
    try:
        ctx = lambda: ValidationContext(onto, "q", neo.principal("raj"), neo.rules, neo.catalog, snap, neo.cfg)
        e = " | ".join(validate(value("sum(branches_transferred)", grounding=ground(neo)), ctx()).errors)
        assert "use `generation`" in e
        e = " | ".join(validate(value("sum(target_assets_pre_deal_000 + failure_cost_to_fdic_000)",
                                      grounding=ground(neo)), ctx()).errors)
        assert "stock" in e
    finally:
        cost.time_semantics, branches.time_semantics, branches.per_period = saved


def test_rules_must_be_approved(neo):
    raw = {"form": "feasibility", "interpretation": "a test of rule approval, precisely stated",
           "universe": {"entity": "deal"}, "parameters": {"b": "rule_param('stress_capital_buffer', 'buffer')"},
           "variables": [{"name": "x", "sort": "Real"}], "constraints": [{"name": "c", "expr": "x >= b"}],
           "target": {"query": "exists"}}
    assert "not approved" in errors(neo, raw)
    raw["parameters"] = {"b": "rule_param('deposit_cap', 'cap')"}
    assert not check(neo, raw).errors


def test_aggregate_parameters_cannot_filter_rows(neo):
    raw = value("count()", parameters={"avg_assets": "avg(target_assets_pre_deal_000)"},
                rows="target_assets_pre_deal_000 > avg_assets", grounding=ground(neo))
    assert "circular" in errors(neo, raw)
    ok = value("count(big)", parameters={"avg_assets": "avg(target_assets_pre_deal_000)"},
               define={"big": "target_assets_pre_deal_000 > avg_assets"}, grounding=ground(neo))
    assert not check(neo, ok).errors


def test_block_constraints_by_word_and_scope(neo):
    assert "con_failed_bank_definition" in errors(neo, value("count()", "deal"), question="How many failed banks?")
    assert "con_acquirer_not_jpm" in errors(neo, value("count()"), question="Who was the buyer?")
    # by use: reading pre-deal assets brings the unit and blank-value rules, whatever the wording
    e = errors(neo, value("sum(target_assets_pre_deal_000)"), question="q")
    assert "con_units" in e and "con_blank_numeric_sorting" in e
    assert "con_units" not in errors(neo, value("count()"), question="q")


def test_join_must_be_an_ontology_relation(neo):
    raw = {"form": "value", "interpretation": "deals joined through a relation that does not exist",
           "universe": {"entity": "deal", "joins": [{"relation": "rel_no_such", "as": "b"}]},
           "target": {"expr": "count()"}}
    assert "rel_no_such" in errors(neo, raw)


def test_strict_shapes(neo):
    assert "aggregate" in errors(neo, value("count()", rows="sum(branches_transferred) > 0"))
    assert "per-row value" in errors(neo, value("branches_transferred"))
    bad_sens = value("count()", parameters={"k": "3"}, sensitivity=[{"parameter": "k", "values": ["x"]}])
    assert "sensitivity" in errors(neo, bad_sens)


def test_cost_needs_approval(neo):
    neo.cfg["governance"]["max_rows_without_approval"] = 100
    r = check(neo, value("count()"), user="demo")
    assert r.model.needs_approval and "955" in r.model.needs_approval
    assert check(neo, value("count()"), user="raj").model.needs_approval is None   # role with large_queries
