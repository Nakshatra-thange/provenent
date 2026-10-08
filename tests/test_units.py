import math

import pytest

from provenant.units import (
    EXACT,
    UnitError,
    format_number,
    format_si,
    get_unit,
    parse_quantity,
    parse_tolerance,
    within_tolerance,
)


@pytest.mark.parametrize(
    ("text", "si", "dimension"),
    [
        ("20 kN", 20000.0, "force"),
        ("20kN", 20000.0, "force"),
        ("1.2e3 Pa", 1200.0, "pressure"),
        ("150 bar", 150e5, "pressure"),
        ("1 psi", 6894.757293168361, "pressure"),
        ("0 degC", 273.15, "temperature"),
        ("-40 degF", 233.15, "temperature"),
        ("32 °F", 273.15, "temperature"),
        ("36 mm", 0.036, "length"),
        ("5%", 0.05, "dimensionless"),
        ("30", 30.0, "dimensionless"),
        ("250 ms", 0.25, "time"),
        ("2 lb/s", 0.90718474, "mass_flow"),
    ],
)
def test_parse_quantity(text, si, dimension):
    q = parse_quantity(text)
    assert math.isclose(q.si, si, rel_tol=1e-12, abs_tol=1e-9)
    assert q.dimension == dimension


def test_bare_number_takes_default_unit():
    assert parse_quantity(2, default_unit="s").si == 2.0
    assert parse_quantity("2", default_unit="s").dimension == "time"


@pytest.mark.parametrize("bad", ["", "kN", "20 parsecs", "abc 3", "nan", True, None])
def test_parse_quantity_rejects(bad):
    with pytest.raises(UnitError):
        parse_quantity(bad)


def test_units_are_case_sensitive():
    assert get_unit("mm").scale == 1e-3
    with pytest.raises(UnitError):
        get_unit("MM")


def test_round_trip_display_unit():
    q = parse_quantity("2900 psi")
    assert q.unit.symbol == "psi"
    assert math.isclose(q.value, 2900)
    assert str(q) == "2900 psi"


def test_canonical_is_unit_independent():
    assert parse_quantity("20 kN").canonical() == parse_quantity("20000 N").canonical()
    assert parse_quantity("20 kN").canonical() != parse_quantity("20 N").canonical()


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (22030.07, "22030"),
        (317.746, "317.7"),
        (0.98, "0.98"),
        (1.0, "1"),
        (12.0, "12"),
        (-3.14159, "-3.142"),
        (1.5e7, "1.500e+07"),
        (0.0001234, "1.234e-04"),
    ],
)
def test_format_number(value, text):
    assert format_number(value) == text


def test_format_si():
    assert format_si(20000, get_unit("kN")) == "20 kN"
    assert format_si(0.05, get_unit("%")) == "5%"
    assert format_si(30, get_unit("1")) == "30"
    assert format_si(273.15, get_unit("degC")) == "0 degC"


def test_tolerances():
    rel = parse_tolerance("2%", "pressure")
    assert rel == ("relative", pytest.approx(0.02))
    assert within_tolerance(118e5, 120e5, rel)
    assert not within_tolerance(117e5, 120e5, rel)

    absolute = parse_tolerance("0.05 mm", "length")
    assert absolute[0] == "absolute"
    assert within_tolerance(0.03604, 0.036, absolute)
    assert not within_tolerance(0.0361, 0.036, absolute)

    # A temperature tolerance is a difference: 1 degC means 1 K, not 274.15 K.
    assert parse_tolerance("1 degC", "temperature") == ("absolute", pytest.approx(1.0))

    assert within_tolerance(1.0, 1.0, EXACT)
    assert not within_tolerance(1.0, 1.001, EXACT)


@pytest.mark.parametrize(
    ("text", "dimension"), [("3 kg", "pressure"), ("0.5", "pressure"), ("-1%", "length"), ("-1 mm", "length")]
)
def test_tolerance_rejects(text, dimension):
    with pytest.raises(UnitError):
        parse_tolerance(text, dimension)
