"""Physical units and quantities.

Every number that enters Provenant is converted to SI on the way in and only
converted back for display. Comparisons, hashes and margins are always computed
in SI, so ``20 kN`` and ``20000 N`` are the same threshold and produce the same
fingerprint.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


class UnitError(ValueError):
    """Raised for unknown units, malformed quantities or dimension mismatches."""


@dataclass(frozen=True)
class Unit:
    symbol: str
    dimension: str
    scale: float
    offset: float = 0.0

    def to_si(self, value: float) -> float:
        return value * self.scale + self.offset

    def from_si(self, value: float) -> float:
        return (value - self.offset) / self.scale


DIMENSIONLESS = "dimensionless"

_REGISTRY: dict[str, Unit] = {}

#: Unit used when a value of a given dimension has no preferred display unit.
SI_UNITS: dict[str, str] = {}


def _define(dimension: str, scale: float, *symbols: str, offset: float = 0.0) -> None:
    unit = Unit(symbols[0], dimension, scale, offset)
    for symbol in symbols:
        if symbol in _REGISTRY:  # pragma: no cover - guards the table below
            raise RuntimeError(f"unit {symbol!r} defined twice")
        _REGISTRY[symbol] = unit
    if scale == 1.0 and offset == 0.0:
        SI_UNITS.setdefault(dimension, symbols[0])


_define(DIMENSIONLESS, 1.0, "1", "-", "ratio")
_define(DIMENSIONLESS, 0.01, "%")

_define("pressure", 1.0, "Pa")
_define("pressure", 1e3, "kPa")
_define("pressure", 1e6, "MPa")
_define("pressure", 1e5, "bar")
_define("pressure", 1e2, "mbar")
_define("pressure", 6894.757293168361, "psi", "psia")
_define("pressure", 101325.0, "atm")

_define("force", 1.0, "N")
_define("force", 1e3, "kN")
_define("force", 1e6, "MN")
_define("force", 4.4482216152605, "lbf")

_define("temperature", 1.0, "K")
_define("temperature", 1.0, "degC", "°C", "C", offset=273.15)
_define("temperature", 5.0 / 9.0, "degF", "°F", "F", offset=273.15 - 32.0 * 5.0 / 9.0)
_define("temperature", 5.0 / 9.0, "degR", "R")

_define("mass", 1.0, "kg")
_define("mass", 1e-3, "g")
_define("mass", 1e3, "t")
_define("mass", 0.45359237, "lb", "lbm")

_define("length", 1.0, "m")
_define("length", 1e-3, "mm")
_define("length", 1e-2, "cm")
_define("length", 1e3, "km")
_define("length", 0.0254, "in")
_define("length", 0.3048, "ft")

_define("area", 1.0, "m^2", "m2", "m²")
_define("area", 1e-6, "mm^2", "mm2", "mm²")
_define("area", 1e-4, "cm^2", "cm2", "cm²")
_define("area", 0.0254**2, "in^2", "in2", "in²")

_define("time", 1.0, "s")
_define("time", 1e-3, "ms")
_define("time", 60.0, "min")
_define("time", 3600.0, "h")

_define("mass_flow", 1.0, "kg/s")
_define("mass_flow", 1e-3, "g/s")
_define("mass_flow", 0.45359237, "lb/s", "lbm/s")

_define("velocity", 1.0, "m/s")
_define("velocity", 0.3048, "ft/s")

_define("molar_mass", 1.0, "kg/kmol", "g/mol")

_DIMENSIONLESS_UNIT = _REGISTRY["1"]


def get_unit(symbol: str | None) -> Unit:
    """Look up a unit by symbol. Symbols are case-sensitive (``mm`` is not ``Mm``)."""
    if symbol is None:
        return _DIMENSIONLESS_UNIT
    key = symbol.strip()
    if key == "":
        return _DIMENSIONLESS_UNIT
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnitError(f"unknown unit {key!r}") from None


def is_unit(symbol: str) -> bool:
    return symbol.strip() in _REGISTRY


def si_unit(dimension: str) -> Unit:
    return get_unit(SI_UNITS.get(dimension, "1"))


@dataclass(frozen=True)
class Quantity:
    """A physical value stored in SI, remembering the unit it was written in."""

    si: float
    unit: Unit

    @property
    def dimension(self) -> str:
        return self.unit.dimension

    @property
    def value(self) -> float:
        return self.unit.from_si(self.si)

    @classmethod
    def of(cls, value: float, unit: str | Unit = "1") -> Quantity:
        u = unit if isinstance(unit, Unit) else get_unit(unit)
        return cls(u.to_si(float(value)), u)

    def canonical(self) -> str:
        """Unit-independent representation used for fingerprints."""
        return f"{self.si!r}[{self.dimension}]"

    def __str__(self) -> str:
        return format_si(self.si, self.unit)


_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_QUANTITY_RE = re.compile(rf"^\s*(?P<num>{_NUMBER})\s*(?P<unit>.*?)\s*$")


def parse_quantity(text: str | int | float, default_unit: str | None = None) -> Quantity:
    """Parse ``"20 kN"``, ``"-5degC"``, ``"1.2e3 Pa"`` or a bare number.

    Bare numbers take ``default_unit`` (dimensionless if not given).
    """
    if isinstance(text, bool):
        raise UnitError(f"expected a quantity, got boolean {text!r}")
    if isinstance(text, (int, float)):
        return Quantity.of(float(text), default_unit or "1")
    if not isinstance(text, str):
        raise UnitError(f"expected a quantity, got {type(text).__name__}")
    match = _QUANTITY_RE.match(text)
    if not match:
        raise UnitError(f"cannot parse quantity {text!r} (expected e.g. '20 kN')")
    value = float(match.group("num"))
    if not math.isfinite(value):  # pragma: no cover - the regex admits only finite literals
        raise UnitError(f"quantity {text!r} is not finite")
    symbol = match.group("unit") or default_unit or ""
    return Quantity.of(value, get_unit(symbol))


def format_number(value: float, significant: int = 4) -> str:
    """Human-friendly number: 4 significant digits, no scientific notation in normal ranges."""
    if value == 0 or not math.isfinite(value):
        return "0" if value == 0 else str(value)
    magnitude = abs(value)
    if magnitude >= 1e6 or magnitude < 1e-3:
        return f"{value:.{significant - 1}e}"
    decimals = max(0, significant - 1 - math.floor(math.log10(magnitude)))
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_si(si_value: float, unit: Unit) -> str:
    number = format_number(unit.from_si(si_value))
    if unit.symbol == "%":
        return f"{number}%"
    if unit.dimension == DIMENSIONLESS:
        return number
    return f"{number} {unit.symbol}"


def parse_tolerance(text: str | int | float, dimension: str) -> tuple[str, float]:
    """Parse a tolerance: ``"2%"`` (relative) or ``"0.5 bar"`` (absolute, same dimension).

    Returns ``("relative", fraction)`` or ``("absolute", si_value)``.
    """
    if isinstance(text, str) and text.strip().endswith("%"):
        q = parse_quantity(text)
        if q.si < 0:
            raise UnitError(f"tolerance {text!r} must not be negative")
        return ("relative", q.si)
    q = parse_quantity(text)
    if q.dimension == DIMENSIONLESS and dimension != DIMENSIONLESS:
        raise UnitError(f"tolerance {text!r} needs a unit or a '%' (parameter is a {dimension})")
    if q.dimension != dimension:
        raise UnitError(f"tolerance {text!r} is a {q.dimension}, parameter is a {dimension}")
    if q.value < 0:
        raise UnitError(f"tolerance {text!r} must not be negative")
    # Absolute tolerances are differences: scale only, never offset (1 degC tolerance = 1 K).
    return ("absolute", float(q.value) * q.unit.scale)


def within_tolerance(a_si: float, b_si: float, tolerance: tuple[str, float]) -> bool:
    kind, amount = tolerance
    difference = abs(a_si - b_si)
    if kind == "relative":
        reference = max(abs(a_si), abs(b_si))
        return difference <= amount * reference + 1e-12 * reference
    return difference <= amount + 1e-12 * max(abs(a_si), abs(b_si), 1.0)


EXACT = ("relative", 1e-9)
