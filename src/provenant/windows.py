"""Events, evaluation windows and the reductions that run inside them.

Test cases define *events* detected from the data (``ignition`` is when chamber
pressure first rises above 10 bar) and *windows* bounded by events with offsets
(``steady_state`` runs from ``ignition + 2 s`` to ``shutdown - 1 s``). Windows
are resolved per run, so the same requirement is judged over the physically
equivalent part of every firing even when timing differs between runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from .expr import compare
from .telemetry import ChannelSpec, Recording, Signal, TelemetryError
from .units import Quantity, UnitError, format_number, parse_quantity


class WindowError(Exception):
    pass


@dataclass(frozen=True)
class EventSpec:
    name: str
    channel: str
    direction: str  # "rises_above" | "falls_below"
    threshold: Quantity
    after: str | None = None

    def describe(self) -> str:
        verb = "rises above" if self.direction == "rises_above" else "falls below"
        after = f" after {self.after}" if self.after else ""
        return f"{self.channel} {verb} {self.threshold}{after}"

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "direction": self.direction,
            "threshold": self.threshold.canonical(),
            "after": self.after,
        }


ABSOLUTE = "@t"


@dataclass(frozen=True)
class Bound:
    ref: str  # "start", "end", an event name, or ABSOLUTE
    offset: float  # seconds
    source: str

    def fingerprint_payload(self) -> list[object]:
        return [self.ref, self.offset]


@dataclass(frozen=True)
class WindowSpec:
    name: str
    start: Bound
    end: Bound

    @property
    def events(self) -> set[str]:
        return {b.ref for b in (self.start, self.end) if b.ref not in ("start", "end", ABSOLUTE)}

    def describe(self) -> str:
        return f"{self.start.source} … {self.end.source}"

    def fingerprint_payload(self) -> dict[str, object]:
        return {"start": self.start.fingerprint_payload(), "end": self.end.fingerprint_payload()}


_BOUND_RE = re.compile(r"^\s*(?P<ref>[A-Za-z_]\w*)\s*(?:(?P<sign>[+-])\s*(?P<offset>.+?))?\s*$")


def _time_offset(text: str, source: str) -> float:
    try:
        q = parse_quantity(text, default_unit="s")
    except UnitError as exc:
        raise WindowError(f"bad time offset in {source!r}: {exc}") from None
    if q.dimension != "time":
        raise WindowError(f"offset in {source!r} must be a time, got {q}")
    return q.si


def parse_bound(text: object) -> Bound:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return Bound(ABSOLUTE, float(text), f"{format_number(float(text))} s")
    source = str(text).strip()
    match = _BOUND_RE.match(source)
    if match:
        ref = match.group("ref")
        offset = 0.0
        if match.group("sign"):
            magnitude = _time_offset(match.group("offset"), source)
            offset = magnitude if match.group("sign") == "+" else -magnitude
        return Bound(ref, offset, source)
    return Bound(ABSOLUTE, _time_offset(source, source), source)


def parse_event(name: str, raw: object) -> EventSpec:
    if not isinstance(raw, dict):
        raise WindowError(f"event '{name}' must be a mapping")
    unknown = set(raw) - {"channel", "rises_above", "falls_below", "after"}
    if unknown:
        raise WindowError(f"event '{name}' has unknown keys: {', '.join(sorted(unknown))}")
    if "channel" not in raw:
        raise WindowError(f"event '{name}' needs a 'channel'")
    directions = [k for k in ("rises_above", "falls_below") if k in raw]
    if len(directions) != 1:
        raise WindowError(f"event '{name}' needs exactly one of 'rises_above' or 'falls_below'")
    direction = directions[0]
    try:
        threshold = parse_quantity(raw[direction])
    except UnitError as exc:
        raise WindowError(f"event '{name}': {exc}") from None
    after = raw.get("after")
    return EventSpec(name, str(raw["channel"]), direction, threshold, None if after is None else str(after))


def parse_window(name: str, raw: object) -> WindowSpec:
    if not isinstance(raw, dict) or "start" not in raw or "end" not in raw:
        raise WindowError(f"window '{name}' needs 'start' and 'end'")
    unknown = set(raw) - {"start", "end"}
    if unknown:
        raise WindowError(f"window '{name}' has unknown keys: {', '.join(sorted(unknown))}")
    return WindowSpec(name, parse_bound(raw["start"]), parse_bound(raw["end"]))


def events_needed(windows: list[WindowSpec], events: dict[str, EventSpec]) -> list[str]:
    """Events (transitively, via ``after``) that the given windows depend on, sorted."""
    needed: set[str] = set()
    stack = [e for w in windows for e in w.events]
    while stack:
        name = stack.pop()
        if name in needed:
            continue
        needed.add(name)
        spec = events.get(name)
        if spec and spec.after:
            stack.append(spec.after)
    return sorted(needed)


# --------------------------------------------------------------------------- resolution


def _find_crossing(signal: Signal, spec: EventSpec, after_t: float) -> float | None:
    threshold = spec.threshold.si
    rising = spec.direction == "rises_above"
    prev_t: float | None = None
    prev_v: float | None = None
    for t, v in zip(signal.t, signal.v, strict=True):
        if t < after_t or v is None:
            continue
        if prev_v is not None and prev_t is not None:
            crossed = (prev_v < threshold <= v) if rising else (prev_v > threshold >= v)
            if crossed:
                fraction = (threshold - prev_v) / (v - prev_v)
                return prev_t + fraction * (t - prev_t)
        prev_t, prev_v = t, v
    return None


class EventResolver:
    def __init__(
        self,
        recording: Recording,
        channels: dict[str, ChannelSpec],
        events: dict[str, EventSpec],
        run_id: str,
    ):
        self.recording = recording
        self.channels = channels
        self.events = events
        self.run_id = run_id
        self._times: dict[str, float] = {}
        self._signals: dict[str, Signal] = {}

    def signal(self, channel: str) -> Signal:
        if channel not in self._signals:
            spec = self.channels.get(channel)
            if spec is None:
                raise WindowError(f"unknown channel '{channel}'")
            self._signals[channel] = self.recording.signal(spec)
        return self._signals[channel]

    def time_of(self, name: str, _stack: tuple[str, ...] = ()) -> float:
        if name in self._times:
            return self._times[name]
        if name in _stack:
            cycle = " -> ".join((*_stack, name))
            raise WindowError(f"events depend on each other in a cycle: {cycle}")
        spec = self.events.get(name)
        if spec is None:
            raise WindowError(f"unknown event '{name}'")
        after_t = self.recording.start
        if spec.after:
            after_t = self.time_of(spec.after, (*_stack, name))
        t = _find_crossing(self.signal(spec.channel), spec, after_t)
        if t is None:
            raise WindowError(f"event '{name}' ({spec.describe()}) was not observed in {self.run_id}")
        self._times[name] = t
        return t

    def bound(self, bound: Bound) -> float:
        if bound.ref == "start":
            base = self.recording.start
        elif bound.ref == "end":
            base = self.recording.end
        elif bound.ref == ABSOLUTE:
            base = 0.0
        else:
            base = self.time_of(bound.ref)
        return base + bound.offset

    def window(self, spec: WindowSpec | None) -> tuple[float, float]:
        if spec is None:
            return self.recording.start, self.recording.end
        t0, t1 = self.bound(spec.start), self.bound(spec.end)
        if t1 <= t0:
            raise WindowError(
                f"window '{spec.name}' is empty in {self.run_id} ({format_number(t0)} s … {format_number(t1)} s)"
            )
        return t0, t1


# --------------------------------------------------------------------------- reductions


@dataclass(frozen=True)
class Reduction:
    value: float
    t_at: float | None  # time of the extreme / reported sample, when meaningful
    samples: int
    missing: int


def _in_window(signal: Signal, t0: float, t1: float) -> tuple[list[float], list[float | None]]:
    ts: list[float] = []
    vs: list[float | None] = []
    for t, v in zip(signal.t, signal.v, strict=True):
        if t0 <= t <= t1:
            ts.append(t)
            vs.append(v)
    return ts, vs


def reduce(signal: Signal, func: str, t0: float, t1: float) -> Reduction:
    ts, vs = _in_window(signal, t0, t1)
    valid = [(t, v) for t, v in zip(ts, vs, strict=True) if v is not None]
    missing = len(vs) - len(valid)
    if not valid:
        raise TelemetryError(f"no valid samples of '{signal.channel}' in the evaluation window")
    if func == "max":
        t, v = max(valid, key=lambda p: p[1])
        return Reduction(v, t, len(valid), missing)
    if func == "min":
        t, v = min(valid, key=lambda p: p[1])
        return Reduction(v, t, len(valid), missing)
    if func == "initial":
        t, v = valid[0]
        return Reduction(v, t, len(valid), missing)
    if func == "final":
        t, v = valid[-1]
        return Reduction(v, t, len(valid), missing)
    if func == "mean":
        # Time-weighted (trapezoidal) mean over intervals where both ends are valid,
        # so irregular sampling or dropouts do not bias the result.
        area = 0.0
        span = 0.0
        for (ta, va), (tb, vb) in pairwise(zip(ts, vs, strict=True)):
            if va is None or vb is None:
                continue
            area += 0.5 * (va + vb) * (tb - ta)
            span += tb - ta
        value = area / span if span > 0 else sum(v for _, v in valid) / len(valid)
        return Reduction(value, None, len(valid), missing)
    raise ValueError(f"unknown aggregate {func!r}")  # pragma: no cover


def duration(signal: Signal, op: str, threshold: float, t0: float, t1: float) -> Reduction:
    """Total time inside [t0, t1] for which ``signal op threshold`` holds.

    Intervals that straddle the threshold contribute the interpolated fraction,
    so the result does not depend on where the samples happen to fall.
    """
    ts, vs = _in_window(signal, t0, t1)
    total = 0.0
    valid = sum(1 for v in vs if v is not None)
    if valid == 0:
        raise TelemetryError(f"no valid samples of '{signal.channel}' in the evaluation window")
    for (ta, va), (tb, vb) in pairwise(zip(ts, vs, strict=True)):
        if va is None or vb is None:
            continue
        a_ok, b_ok = compare(va, op, threshold), compare(vb, op, threshold)
        dt = tb - ta
        if a_ok and b_ok:
            total += dt
        elif a_ok != b_ok and vb != va:
            crossing = (threshold - va) / (vb - va)
            total += dt * (crossing if a_ok else 1.0 - crossing)
    return Reduction(total, None, valid, len(vs) - valid)
