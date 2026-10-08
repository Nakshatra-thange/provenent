import pytest

from provenant.expr import Aggregate, ConditionError, Duration, ValueRef, parse_condition


def test_aggregate_with_window():
    c = parse_condition("mean(thrust) >= 20 kN during steady_state")
    assert c.measure == Aggregate("mean", "thrust")
    assert c.op == ">="
    assert c.threshold.si == 20000
    assert c.window == "steady_state"
    assert c.channels == {"thrust"}
    assert c.is_telemetry


def test_value_reference():
    c = parse_condition("value(isp_vac) >= 315 s")
    assert c.measure == ValueRef("isp_vac")
    assert not c.is_telemetry
    assert c.channels == frozenset()


def test_duration():
    c = parse_condition("duration(chamber_pressure >= 108 bar) >= 25 s")
    assert isinstance(c.measure, Duration)
    assert c.measure.threshold.si == 108e5
    assert c.threshold.si == 25


@pytest.mark.parametrize(
    "source",
    ["max(T) <= -5 degC", "max(T) <= -5degC", "max(T)<=-5 degC", "  max( T )  <=  -5  degC  "],
)
def test_negative_and_spacing(source):
    c = parse_condition(source)
    assert c.threshold.si == pytest.approx(268.15)


def test_canonical_ignores_units_and_spacing():
    a = parse_condition("max(p) <= 150 bar")
    b = parse_condition("max( p )<=15000 kPa")
    assert a.canonical() == b.canonical()
    assert a.canonical() != parse_condition("max(p) < 150 bar").canonical()


def test_margin_sign_convention():
    upper = parse_condition("max(p) <= 100 bar")
    assert upper.margin(90e5) == pytest.approx(0.1)
    assert upper.margin(110e5) == pytest.approx(-0.1)
    lower = parse_condition("mean(f) >= 20 kN")
    assert lower.margin(22e3) == pytest.approx(0.1)
    assert lower.holds(20e3) and not lower.holds(19999)
    assert parse_condition("max(x) <= 0").margin(1) is None
    assert parse_condition("max(x) < 5").holds(4) and parse_condition("max(x) > 5").holds(6)


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("", "empty condition"),
        ("max(p)", "comparison"),
        ("max(p) == 3 bar", "not supported"),
        ("avg(p) <= 3 bar", "unknown function 'avg'"),
        ("max(p) <= 3 furlongs", "unknown unit"),
        ("max(p) <= bar", "expected a number"),
        ("max(p <= 3 bar", "expected ')'"),
        ("max(p) <= 3 bar during", "window name"),
        ("max(p) <= 3 bar during w extra", "unexpected"),
        ("value(x) <= 3 bar during w", "cannot have a 'during'"),
        ("duration(p >= 3 bar) >= 5 kg", "compared against a time"),
        ("max(p) <= -3bar psi", "unexpected text after the quantity"),
    ],
)
def test_errors_are_specific(source, fragment):
    with pytest.raises(ConditionError) as info:
        parse_condition(source)
    assert fragment in str(info.value)


def test_error_points_at_position():
    with pytest.raises(ConditionError) as info:
        parse_condition("max(p) <= 3 furlongs")
    assert info.value.position == len("max(p) <= 3 ")


def test_non_text_condition_rejected():
    with pytest.raises(ConditionError, match="must be text"):
        parse_condition(42)  # type: ignore[arg-type]
