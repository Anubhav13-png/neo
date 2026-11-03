"""The expression language rejects anything that could run code; unit algebra catches scale and dimension errors."""
import pytest

from neo.dsl import ExprError, parse, refs
from neo.semantic.units import SCALAR, Unit, combine, parse_unit
from neo.semantic.validator import T, add_units, div_units, mul_units
from fractions import Fraction


@pytest.mark.parametrize("bad", ["__import__('os')", "a.b.c", "x[0]", "lambda: 1", "[i for i in x]", "(a := 1)",
                                 "f(*args)", "obj.__class__", "open('x').read()", "{1: 2}"])
def test_rejects_code(bad):
    with pytest.raises(ExprError):
        parse(bad)


def test_alias_field_and_refs():
    e = parse("cust.segment == 'SME' and balance_usd > max(1, pd)")
    names, calls = refs(e)
    assert names == {"cust.segment", "balance_usd", "pd"} and calls == {"max"}


def test_unit_parsing():
    assert parse_unit("USD_thousands") == Unit("USD", 3)
    assert parse_unit("percent") == Unit(None, -2) and parse_unit("bp") == Unit(None, -4)
    assert parse_unit("probability") == SCALAR and parse_unit("text") is None
    with pytest.raises(ValueError):
        parse_unit("furlongs")


def test_dimension_algebra():
    assert combine("USD", "USD", divide=False) == "USD^2"
    assert combine(combine("USD", "USD", False), "USD", divide=True) == "USD"
    assert combine("USD", "USD", divide=True) is None
    assert combine("USD", "count", divide=True) == "USD/count"


def num(unit, lit=None):
    return T("number", unit, literal=Fraction(lit) if lit is not None else None)


def test_scale_and_dimension_errors():
    thousands, dollars, billions = Unit("USD", 3), Unit("USD", 0), Unit("USD", 9)
    assert add_units(num(thousands), num(dollars))[1] is not None            # thousands + dollars
    assert add_units(num(Unit(None, -2)), num(SCALAR))[1] is not None         # percent + ratio
    assert add_units(num(dollars), num(Unit("count")))[1] is not None         # dollars + count
    assert add_units(num(dollars), num(SCALAR, 5))[1] is None                 # a literal adopts the unit
    assert div_units(num(thousands), num(SCALAR, 1e6))[0] == billions         # thousands / 1e6 = billions
    assert mul_units(num(Unit(None, -4)), num(SCALAR, 1)) == Unit(None, -4)
    u, problem = div_units(num(thousands), num(billions))
    assert problem is not None                                                # ratio off by 10^-6
