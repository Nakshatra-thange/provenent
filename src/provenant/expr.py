"""The requirement condition language.

A condition is a single comparison that a requirement must satisfy::

    max(chamber_pressure) <= 150 bar
    mean(thrust) >= 20 kN during steady_state
    duration(chamber_pressure >= 108 bar) >= 25 s
    value(isp_vac) >= 315 s

Grammar::

    condition  := measure CMP quantity [ "during" NAME ]
    measure    := AGG "(" NAME ")"
                | "value" "(" NAME ")"
                | "duration" "(" NAME CMP quantity ")"
    AGG        := max | min | mean | initial | final
    CMP        := <= | >= | < | >

Conditions are parsed once, never ``eval``-ed, and reduced to a canonical
SI form for fingerprinting so cosmetic edits do not invalidate evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .units import Quantity, UnitError, is_unit, parse_quantity

AGGREGATES = ("max", "min", "mean", "initial", "final")
COMPARATORS = ("<=", ">=", "<", ">")
_KEYWORDS = {"during"}


class ConditionError(ValueError):
    def __init__(self, message: str, source: str = "", position: int | None = None):
        self.message = message
        self.source = source
        self.position = position
        text = message
        if source:
            text = f"{message}\n    {source}"
            if position is not None:
                text += "\n    " + " " * position + "^"
        super().__init__(text)


@dataclass(frozen=True)
class Aggregate:
    func: str
    channel: str

    def canonical(self) -> str:
        return f"{self.func}({self.channel})"


@dataclass(frozen=True)
class ValueRef:
    name: str

    def canonical(self) -> str:
        return f"value({self.name})"


@dataclass(frozen=True)
class Duration:
    channel: str
    op: str
    threshold: Quantity

    def canonical(self) -> str:
        return f"duration({self.channel}{self.op}{self.threshold.canonical()})"


Measure = Aggregate | ValueRef | Duration


def compare(lhs: float, op: str, rhs: float) -> bool:
    if op == "<=":
        return lhs <= rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<":
        return lhs < rhs
    if op == ">":
        return lhs > rhs
    raise ValueError(f"unknown comparator {op!r}")  # pragma: no cover


@dataclass(frozen=True)
class Condition:
    measure: Measure
    op: str
    threshold: Quantity
    window: str | None
    source: str

    @property
    def is_telemetry(self) -> bool:
        return not isinstance(self.measure, ValueRef)

    @property
    def channels(self) -> frozenset[str]:
        if isinstance(self.measure, (Aggregate, Duration)):
            return frozenset({self.measure.channel})
        return frozenset()

    def canonical(self) -> str:
        window = f" during {self.window}" if self.window else ""
        return f"{self.measure.canonical()} {self.op} {self.threshold.canonical()}{window}"

    def holds(self, measured_si: float) -> bool:
        return compare(measured_si, self.op, self.threshold.si)

    def margin(self, measured_si: float) -> float | None:
        """Signed relative margin; positive means compliant with room to spare."""
        reference = abs(self.threshold.si)
        if reference == 0:
            return None
        if self.op in ("<=", "<"):
            return (self.threshold.si - measured_si) / reference
        return (measured_si - self.threshold.si) / reference

    def __str__(self) -> str:
        return self.source


# --------------------------------------------------------------------------- parsing

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<op><=|>=|==|!=|<|>|=)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<word>[^\s()<>=!,]+)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if not match:  # pragma: no cover - the word rule matches everything else
            raise ConditionError("unexpected character", source, pos)
        kind = match.lastgroup or ""
        if kind != "ws":
            tokens.append(_Token(kind, match.group(), pos))
        pos = match.end()
    tokens.append(_Token("end", "", len(source)))
    return tokens


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = _tokenize(source)
        self.i = 0

    @property
    def tok(self) -> _Token:
        return self.tokens[self.i]

    def error(self, message: str, token: _Token | None = None) -> ConditionError:
        t = token or self.tok
        return ConditionError(message, self.source, t.pos)

    def advance(self) -> _Token:
        t = self.tok
        self.i += 1
        return t

    def expect(self, kind: str, what: str) -> _Token:
        if self.tok.kind != kind:
            found = self.tok.text or "end of condition"
            raise self.error(f"expected {what}, found {found!r}")
        return self.advance()

    def name(self, what: str) -> str:
        t = self.tok
        if t.kind != "word" or not _NAME_RE.match(t.text) or t.text in _KEYWORDS:
            raise self.error(f"expected {what}")
        self.advance()
        return t.text

    def comparator(self) -> str:
        t = self.tok
        if t.kind != "op":
            raise self.error("expected a comparison (<=, >=, < or >)")
        if t.text not in COMPARATORS:
            raise self.error(
                f"'{t.text}' is not supported; use <=, >=, < or > (exact equality is not meaningful for measured data)"
            )
        self.advance()
        return t.text

    def quantity(self) -> Quantity:
        start = self.tok
        if start.kind == "number":
            self.advance()
            unit = ""
            if self.tok.kind == "word" and self.tok.text not in _KEYWORDS:
                if not is_unit(self.tok.text):
                    raise self.error(f"unknown unit {self.tok.text!r}")
                unit = self.advance().text
            return parse_quantity(f"{start.text} {unit}")
        if start.kind == "word" and start.text not in _KEYWORDS:
            # Handles signed or glued forms such as "-5", "-5degC".
            try:
                q = parse_quantity(start.text)
            except UnitError:
                raise self.error("expected a number with an optional unit, e.g. '20 kN'") from None
            self.advance()
            if self.tok.kind == "word" and self.tok.text not in _KEYWORDS:
                if q.unit.symbol != "1":
                    raise self.error("unexpected text after the quantity")
                if not is_unit(self.tok.text):
                    raise self.error(f"unknown unit {self.tok.text!r}")
                q = parse_quantity(f"{start.text} {self.advance().text}")
            return q
        raise self.error("expected a number with an optional unit, e.g. '20 kN'")

    def measure(self) -> Measure:
        func_tok = self.tok
        func = self.name("a measure such as max(channel), value(name) or duration(...)")
        self.expect("lparen", "'('")
        measure: Measure
        if func in AGGREGATES:
            measure = Aggregate(func, self.name("a channel name"))
        elif func == "value":
            measure = ValueRef(self.name("a design parameter or analysis output name"))
        elif func == "duration":
            channel = self.name("a channel name")
            op = self.comparator()
            measure = Duration(channel, op, self.quantity())
        else:
            options = ", ".join((*AGGREGATES, "value", "duration"))
            raise self.error(f"unknown function {func!r}; expected one of {options}", func_tok)
        self.expect("rparen", "')'")
        return measure

    def condition(self) -> Condition:
        if self.tok.kind == "end":
            raise self.error("empty condition")
        measure = self.measure()
        op = self.comparator()
        threshold = self.quantity()
        window = None
        if self.tok.kind == "word" and self.tok.text == "during":
            self.advance()
            window = self.name("a window name after 'during'")
        if self.tok.kind != "end":
            raise self.error(f"unexpected {self.tok.text!r}")
        if isinstance(measure, ValueRef) and window:
            raise ConditionError("value(...) conditions cannot have a 'during' window", self.source)
        if isinstance(measure, Duration) and threshold.dimension != "time":
            raise ConditionError(f"duration(...) is compared against a time, got {threshold}", self.source)
        return Condition(measure, op, threshold, window, " ".join(self.source.split()))


def parse_condition(source: str) -> Condition:
    if not isinstance(source, str):
        raise ConditionError(f"condition must be text, got {type(source).__name__}")
    return _Parser(source.strip()).condition()
