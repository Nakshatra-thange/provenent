"""Loading test-run recordings and resolving logical channels to raw columns.

Raw data acquisition columns (``PT-101``, ``LC-01``) are mapped to logical
channels (``chamber_pressure``, ``thrust``) in ``channels.yaml``. A channel may
list ``aliases`` so a column rename between test campaigns does not break
older runs, and may carry a linear calibration (``scale``/``offset``, applied in
raw units before the unit conversion to SI).
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from .units import Unit


class TelemetryError(Exception):
    pass


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    column: str
    unit: Unit
    scale: float = 1.0
    offset: float = 0.0
    aliases: tuple[str, ...] = ()
    description: str = ""

    def fingerprint_payload(self) -> dict[str, object]:
        # Description is cosmetic and deliberately excluded.
        return {
            "column": self.column,
            "aliases": list(self.aliases),
            "unit": [self.unit.dimension, self.unit.scale, self.unit.offset],
            "scale": self.scale,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class TimeSpec:
    column: str
    unit: Unit
    aliases: tuple[str, ...] = ()

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "column": self.column,
            "aliases": list(self.aliases),
            "unit": [self.unit.scale, self.unit.offset],
        }


@dataclass
class Signal:
    """A logical channel in SI units on the recording's time base."""

    channel: str
    column: str
    unit: Unit  # display unit (the channel's configured unit)
    t: list[float]
    v: list[float | None]

    @property
    def valid_count(self) -> int:
        return sum(1 for x in self.v if x is not None)


_MISSING = {"", "nan", "NaN", "NAN", "null", "NULL", "None", "NA", "N/A"}


def _parse_cell(text: str, column: str, row: int, path: Path) -> float | None:
    text = text.strip()
    if text in _MISSING:
        return None
    try:
        value = float(text)
    except ValueError:
        raise TelemetryError(f"{path.name}: row {row}, column {column!r}: {text!r} is not a number") from None
    return value if math.isfinite(value) else None


@dataclass
class Recording:
    path: Path
    sha256: str
    header: list[str]
    columns: dict[str, list[float | None]]
    time: list[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, time_spec: TimeSpec) -> Recording:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise TelemetryError(f"data file not found: {path}") from None
        sha = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise TelemetryError(f"{path.name}: not UTF-8 encoded text") from None

        lines = (line for line in io.StringIO(text) if not line.lstrip().startswith("#"))
        reader = csv.reader(lines)
        header: list[str] | None = None
        rows: list[list[str]] = []
        for record in reader:
            if not record or all(not cell.strip() for cell in record):
                continue
            if header is None:
                header = [cell.strip() for cell in record]
                continue
            rows.append(record)
        if header is None:
            raise TelemetryError(f"{path.name}: file has no header row")
        duplicates = sorted({h for h in header if header.count(h) > 1})
        if duplicates:
            raise TelemetryError(f"{path.name}: duplicate column names {duplicates}")
        if not rows:
            raise TelemetryError(f"{path.name}: file has a header but no data rows")

        columns: dict[str, list[float | None]] = {name: [] for name in header}
        for index, record in enumerate(rows, start=2):
            if len(record) != len(header):
                raise TelemetryError(f"{path.name}: row {index} has {len(record)} fields, header has {len(header)}")
            for name, cell in zip(header, record, strict=True):
                columns[name].append(_parse_cell(cell, name, index, path))

        recording = cls(path, sha, header, columns)
        time_column = recording._find_column(time_spec.column, time_spec.aliases, "time")
        time_values: list[float] = []
        previous = -math.inf
        for index, value in enumerate(columns[time_column], start=2):
            if value is None:
                raise TelemetryError(f"{path.name}: row {index} has no time value")
            t = time_spec.unit.to_si(value)
            if t <= previous:
                raise TelemetryError(f"{path.name}: time must be strictly increasing (row {index}: {value})")
            previous = t
            time_values.append(t)
        recording.time = time_values
        return recording

    def _find_column(self, column: str, aliases: tuple[str, ...], what: str) -> str:
        for candidate in (column, *aliases):
            if candidate in self.columns:
                return candidate
        names = ", ".join([column, *aliases])
        available = ", ".join(self.header)
        raise TelemetryError(f"{self.path.name}: no column for {what} (looked for {names}; available: {available})")

    def signal(self, spec: ChannelSpec) -> Signal:
        column = self._find_column(spec.column, spec.aliases, f"channel '{spec.name}'")
        values = [
            None if raw is None else spec.unit.to_si(raw * spec.scale + spec.offset) for raw in self.columns[column]
        ]
        return Signal(spec.name, column, spec.unit, self.time, values)

    @property
    def start(self) -> float:
        return self.time[0]

    @property
    def end(self) -> float:
        return self.time[-1]
