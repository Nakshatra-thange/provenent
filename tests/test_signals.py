"""Telemetry loading, events, windows and reductions on hand-built recordings."""

from pathlib import Path

import pytest

from provenant.telemetry import ChannelSpec, Recording, TelemetryError, TimeSpec
from provenant.units import get_unit, parse_quantity
from provenant.windows import (
    EventResolver,
    WindowError,
    duration,
    parse_bound,
    parse_event,
    parse_window,
    reduce,
)

TIME = TimeSpec("t", get_unit("s"))
P = ChannelSpec("p", "P", get_unit("bar"))


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "run.csv"
    path.write_text(text, encoding="utf-8")
    return path


def ramp(tmp_path: Path) -> Recording:
    # p: 0 for t<1, ramps to 10 bar at t=2, holds to t=6, falls to 0 at t=7.
    rows = ["# comment line", "t,P,Q"]
    for i in range(0, 81):
        t = i / 10
        p = 0 if t < 1 else min(10.0, (t - 1) * 10) if t < 6 else max(0.0, 10 - (t - 6) * 10)
        q = "" if 3.0 <= t <= 3.2 else f"{t:.1f}"
        rows.append(f"{t:.1f},{p:.3f},{q}")
    return Recording.load(write(tmp_path, "\n".join(rows) + "\n"), TIME)


def test_load_and_missing_values(tmp_path):
    rec = ramp(tmp_path)
    assert rec.header == ["t", "P", "Q"]
    assert len(rec.time) == 81
    q = rec.signal(ChannelSpec("q", "Q", get_unit("s")))
    assert q.valid_count == 81 - 3
    assert len(rec.sha256) == 64


def test_alias_and_calibration(tmp_path):
    rec = ramp(tmp_path)
    spec = ChannelSpec("p", "PT-101", get_unit("bar"), scale=2.0, offset=1.0, aliases=("P",))
    sig = rec.signal(spec)
    assert sig.column == "P"
    assert sig.v[-1] == pytest.approx(1e5)  # (0 * 2 + 1) bar


def test_missing_column_lists_available(tmp_path):
    rec = ramp(tmp_path)
    with pytest.raises(TelemetryError, match="available: t, P, Q"):
        rec.signal(ChannelSpec("x", "X", get_unit("bar")))


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "no header"),
        ("t,P\n", "no data rows"),
        ("t,P\n0,1\n1,2,3\n", "row 3 has 3 fields"),
        ("t,P\n0,1\n0,2\n", "strictly increasing"),
        ("t,P\n0,abc\n", "not a number"),
        ("t,P,P\n0,1,2\n", "duplicate column"),
        ("P\n1\n", "no column for time"),
        ("t,P\n,1\n", "no time value"),
    ],
)
def test_malformed_files(tmp_path, text, fragment):
    with pytest.raises(TelemetryError, match=fragment):
        Recording.load(write(tmp_path, text), TIME)


def test_missing_file_and_bad_encoding(tmp_path):
    with pytest.raises(TelemetryError, match="not found"):
        Recording.load(tmp_path / "nope.csv", TIME)
    path = tmp_path / "bin.csv"
    path.write_bytes(b"t,P\n0,\xff\xfe\n")
    with pytest.raises(TelemetryError, match="UTF-8"):
        Recording.load(path, TIME)


def test_bounds():
    assert parse_bound("ignition + 2 s").ref == "ignition"
    assert parse_bound("ignition + 2 s").offset == 2.0
    assert parse_bound("shutdown - 500 ms").offset == pytest.approx(-0.5)
    assert parse_bound("start").offset == 0.0
    assert parse_bound(5).offset == 5.0
    assert parse_bound("12.5 s").offset == 12.5
    with pytest.raises(WindowError):
        parse_bound("ignition + 3 bar")
    with pytest.raises(WindowError):
        parse_bound("ignition + lots")


def test_event_validation():
    with pytest.raises(WindowError, match="exactly one"):
        parse_event("x", {"channel": "p", "rises_above": "1 bar", "falls_below": "1 bar"})
    with pytest.raises(WindowError, match="unknown keys"):
        parse_event("x", {"channel": "p", "rises_above": "1 bar", "when": 3})
    with pytest.raises(WindowError, match="needs a 'channel'"):
        parse_event("x", {"rises_above": "1 bar"})
    with pytest.raises(WindowError, match="must be a mapping"):
        parse_event("x", "p > 1 bar")
    with pytest.raises(WindowError, match="needs 'start' and 'end'"):
        parse_window("w", {"start": "start"})
    with pytest.raises(WindowError, match="unknown keys"):
        parse_window("w", {"start": "start", "end": "end", "step": 1})


def resolver(rec, events):
    return EventResolver(rec, {"p": P}, {k: parse_event(k, v) for k, v in events.items()}, "RUN-T")


def test_events_and_window_resolution(tmp_path):
    rec = ramp(tmp_path)
    r = resolver(
        rec,
        {
            "ign": {"channel": "p", "rises_above": "5 bar"},
            "cut": {"channel": "p", "falls_below": "5 bar", "after": "ign"},
        },
    )
    assert r.time_of("ign") == pytest.approx(1.5)  # interpolated between samples
    assert r.time_of("cut") == pytest.approx(6.5)
    window = parse_window("ss", {"start": "ign + 1 s", "end": "cut - 1 s"})
    assert r.window(window) == pytest.approx((2.5, 5.5))
    assert r.window(None) == (0.0, 8.0)
    assert r.window(parse_window("abs", {"start": 1, "end": "end"})) == pytest.approx((1.0, 8.0))


def test_event_not_observed_and_cycles(tmp_path):
    rec = ramp(tmp_path)
    r = resolver(rec, {"big": {"channel": "p", "rises_above": "50 bar"}})
    with pytest.raises(WindowError, match="was not observed in RUN-T"):
        r.time_of("big")
    with pytest.raises(WindowError, match="unknown event"):
        r.time_of("nope")
    r = resolver(
        rec,
        {
            "a": {"channel": "p", "rises_above": "5 bar", "after": "b"},
            "b": {"channel": "p", "rises_above": "5 bar", "after": "a"},
        },
    )
    with pytest.raises(WindowError, match="cycle"):
        r.time_of("a")


def test_empty_window(tmp_path):
    rec = ramp(tmp_path)
    r = resolver(rec, {"ign": {"channel": "p", "rises_above": "5 bar"}})
    with pytest.raises(WindowError, match="is empty"):
        r.window(parse_window("w", {"start": "ign + 2 s", "end": "ign + 1 s"}))


def test_reductions(tmp_path):
    rec = ramp(tmp_path)
    sig = rec.signal(P)
    peak = reduce(sig, "max", 0, 8)
    assert peak.value == pytest.approx(10e5)
    assert peak.t_at == pytest.approx(2.0)  # first occurrence of the maximum
    assert reduce(sig, "min", 2, 5).value == pytest.approx(10e5)
    assert reduce(sig, "mean", 2, 6).value == pytest.approx(10e5)
    assert reduce(sig, "initial", 0, 8).value == 0
    assert reduce(sig, "final", 0, 6.95).t_at == pytest.approx(6.9)
    with pytest.raises(TelemetryError, match="no valid samples"):
        reduce(sig, "max", 100, 200)
    with pytest.raises(TelemetryError, match="no valid samples"):
        duration(sig, ">=", 1.0, 100, 200)


def test_mean_is_time_weighted(tmp_path):
    # Irregular sampling: a naive sample mean would be (0+0+0+10)/4 = 2.5.
    path = write(tmp_path, "t,P\n0,0\n0.1,0\n0.2,0\n10,10\n")
    sig = Recording.load(path, TIME).signal(P)
    assert reduce(sig, "mean", 0, 10).value == pytest.approx(5e5 * 9.8 / 10)


def test_mean_skips_dropouts(tmp_path):
    path = write(tmp_path, "t,P\n0,1\n1,1\n2,\n3,1\n4,1\n")
    sig = Recording.load(path, TIME).signal(P)
    r = reduce(sig, "mean", 0, 4)
    assert r.value == pytest.approx(1e5)
    assert r.missing == 1


def test_duration_interpolates_crossings(tmp_path):
    rec = ramp(tmp_path)
    sig = rec.signal(P)
    # p >= 5 bar from t=1.5 to t=6.5 exactly, independent of the 0.1 s sampling.
    assert duration(sig, ">=", parse_quantity("5 bar").si, 0, 8).value == pytest.approx(5.0)
    assert duration(sig, ">=", parse_quantity("5 bar").si, 2, 4).value == pytest.approx(2.0)
    assert duration(sig, ">", parse_quantity("50 bar").si, 0, 8).value == 0
