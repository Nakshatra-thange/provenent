"""Loading and validating a Provenant project directory.

Layout::

    project.yaml        name, program, analysis model
    requirements.yaml   requirement tree with machine-checkable conditions
    test_cases.yaml     test cases: what they verify, events, windows, sensitivities
    channels.yaml       logical channel -> raw column mapping and calibration
    design.yaml         current design parameters (the configuration under test)
    runs/*.yaml         one file per test run, pointing at its data file

Validation collects *every* problem before reporting, so a broken project gets
one complete list instead of a fix-one-rerun loop.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .expr import Aggregate, Condition, ConditionError, Duration, ValueRef, parse_condition
from .physics import MODELS, Model
from .telemetry import ChannelSpec, TimeSpec
from .units import EXACT, Quantity, UnitError, get_unit, parse_quantity, parse_tolerance
from .windows import EventSpec, WindowError, WindowSpec, parse_event, parse_window

METHODS = ("test", "analysis", "inspection")


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.where}: {self.message}"


class ProjectError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        errors = [i for i in issues if i.severity == "error"]
        lines = "\n".join(f"  - {i.where}: {i.message}" for i in errors)
        super().__init__(f"project has {len(errors)} error(s):\n{lines}")


@dataclass(frozen=True)
class Requirement:
    id: str
    title: str
    text: str = ""
    parent: str | None = None
    method: str | None = None
    condition: Condition | None = None


@dataclass(frozen=True)
class TestCase:
    __test__ = False  # not a pytest test class

    id: str
    title: str
    verifies: tuple[str, ...]
    configuration: tuple[str, ...]
    events: dict[str, EventSpec]
    windows: dict[str, WindowSpec]
    description: str = ""


@dataclass(frozen=True)
class DesignParam:
    name: str
    quantity: Quantity
    tolerance: tuple[str, float] = EXACT
    source: str = ""


@dataclass(frozen=True)
class Run:
    id: str
    test_case: str
    date: str
    data: Path
    article: str
    configuration: dict[str, Quantity]
    notes: str = ""
    void: str | None = None
    meta_path: Path | None = None


@dataclass
class Project:
    root: Path
    name: str
    program: str
    model: Model | None
    requirements: dict[str, Requirement]
    test_cases: dict[str, TestCase]
    channels: dict[str, ChannelSpec]
    time: TimeSpec
    design: dict[str, DesignParam]
    runs: dict[str, Run]
    warnings: list[Issue] = field(default_factory=list)
    overrides: dict[str, Quantity] = field(default_factory=dict)

    # ---------------------------------------------------------------- structure

    def children(self, requirement_id: str) -> list[Requirement]:
        return [r for r in self.requirements.values() if r.parent == requirement_id]

    def roots(self) -> list[Requirement]:
        return [r for r in self.requirements.values() if r.parent is None]

    def depth(self, requirement_id: str) -> int:
        depth, current = 0, self.requirements[requirement_id].parent
        while current is not None:
            depth += 1
            current = self.requirements[current].parent
        return depth

    def tree_order(self) -> list[Requirement]:
        ordered: list[Requirement] = []

        def visit(req: Requirement) -> None:
            ordered.append(req)
            for child in self.children(req.id):
                visit(child)

        for root in self.roots():
            visit(root)
        return ordered

    def test_cases_for(self, requirement_id: str) -> list[TestCase]:
        return [tc for tc in self.test_cases.values() if requirement_id in tc.verifies]

    def runs_for(self, test_case_id: str) -> list[Run]:
        """Runs of a test case, oldest first (by date, then id)."""
        runs = [r for r in self.runs.values() if r.test_case == test_case_id]
        return sorted(runs, key=lambda r: (r.date, r.id))

    def value_dimension(self, name: str) -> str | None:
        if self.model and name in self.model.outputs:
            return self.model.outputs[name][0]
        if name in self.design:
            return self.design[name].quantity.dimension
        return None

    def with_overrides(self, overrides: dict[str, Quantity]) -> Project:
        """A copy of the project with design parameters replaced (for what-if analysis)."""
        design = dict(self.design)
        for name, quantity in overrides.items():
            if name not in design:
                raise ProjectError([Issue("error", "--set", f"unknown design parameter '{name}'")])
            current = design[name]
            if quantity.dimension != current.quantity.dimension:
                message = f"'{name}' is a {current.quantity.dimension}, got {quantity}"
                raise ProjectError([Issue("error", "--set", message)])
            design[name] = dataclasses.replace(current, quantity=quantity, source="what-if")
        return dataclasses.replace(self, design=design, overrides={**self.overrides, **overrides})

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, root: str | Path) -> Project:
        return _Loader(Path(root)).load()


def find_project_root(start: str | Path) -> Path:
    """Walk up from ``start`` to the directory containing ``project.yaml``."""
    path = Path(start).resolve()
    for candidate in (path, *path.parents):
        if (candidate / "project.yaml").is_file():
            return candidate
    raise ProjectError([Issue("error", str(path), "no project.yaml found here or in any parent")])


def _date_text(value: Any) -> str:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


class _Loader:
    def __init__(self, root: Path):
        self.root = root
        self.issues: list[Issue] = []

    def error(self, where: str, message: str) -> None:
        self.issues.append(Issue("error", where, message))

    def warn(self, where: str, message: str) -> None:
        self.issues.append(Issue("warning", where, message))

    def read_yaml(self, relative: str, required: bool = True) -> Any:
        path = self.root / relative
        if not path.is_file():
            if required:
                self.error(relative, "file not found")
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                return yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" (line {mark.line + 1})" if mark else ""
            self.error(relative, f"invalid YAML{location}: {getattr(exc, 'problem', exc)}")
            return None

    def mapping(self, value: Any, where: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.error(where, f"expected a mapping, got {type(value).__name__}")
            return {}
        return value

    def items(self, value: Any, key: str, where: str) -> list[dict[str, Any]]:
        container = self.mapping(value, where)
        entries = container.get(key, [])
        if entries is None:
            return []
        if not isinstance(entries, list):
            self.error(where, f"'{key}' must be a list")
            return []
        good: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                self.error(where, f"{key}[{index}] must be a mapping")
            elif not entry.get("id"):
                self.error(where, f"{key}[{index}] has no 'id'")
            else:
                good.append(entry)
        return good

    # ---------------------------------------------------------------- sections

    def load(self) -> Project:
        if not (self.root / "project.yaml").is_file():
            raise ProjectError([Issue("error", str(self.root), "no project.yaml in this directory")])
        meta = self.mapping(self.read_yaml("project.yaml"), "project.yaml")
        model = None
        if meta.get("model"):
            model = MODELS.get(str(meta["model"]))
            if model is None:
                available = ", ".join(MODELS)
                self.error("project.yaml", f"unknown model '{meta['model']}' (available: {available})")

        channels, time_spec = self.load_channels()
        design = self.load_design()
        requirements = self.load_requirements()
        test_cases = self.load_test_cases()
        runs = self.load_runs(design)

        project = Project(
            root=self.root,
            name=str(meta.get("name", self.root.name)),
            program=str(meta.get("program", "")),
            model=model,
            requirements=requirements,
            test_cases=test_cases,
            channels=channels,
            time=time_spec,
            design=design,
            runs=runs,
        )
        self.cross_check(project)
        if any(i.severity == "error" for i in self.issues):
            raise ProjectError(self.issues)
        project.warnings = [i for i in self.issues if i.severity == "warning"]
        return project

    def load_channels(self) -> tuple[dict[str, ChannelSpec], TimeSpec]:
        raw = self.mapping(self.read_yaml("channels.yaml"), "channels.yaml")
        time_raw = self.mapping(raw.get("time", {}), "channels.yaml: time")
        time_spec = TimeSpec("time", get_unit("s"))
        try:
            time_spec = TimeSpec(
                str(time_raw.get("column", "time")),
                get_unit(time_raw.get("unit", "s")),
                tuple(str(a) for a in time_raw.get("aliases", []) or []),
            )
            if time_spec.unit.dimension != "time":
                self.error("channels.yaml: time", f"unit '{time_spec.unit.symbol}' is not a time unit")
        except UnitError as exc:
            self.error("channels.yaml: time", str(exc))

        channels: dict[str, ChannelSpec] = {}
        for name, spec in self.mapping(raw.get("channels", {}), "channels.yaml: channels").items():
            where = f"channels.yaml: {name}"
            spec = self.mapping(spec, where)
            unknown = set(spec) - {"column", "unit", "scale", "offset", "aliases", "description"}
            if unknown:
                self.error(where, f"unknown keys: {', '.join(sorted(unknown))}")
            if "column" not in spec:
                self.error(where, "needs a 'column'")
                continue
            try:
                scale = float(spec.get("scale", 1.0))
                offset = float(spec.get("offset", 0.0))
                if scale == 0:
                    raise ValueError("scale must not be zero")
                channels[str(name)] = ChannelSpec(
                    name=str(name),
                    column=str(spec["column"]),
                    unit=get_unit(spec.get("unit")),
                    scale=scale,
                    offset=offset,
                    aliases=tuple(str(a) for a in spec.get("aliases", []) or []),
                    description=str(spec.get("description", "")),
                )
            except (UnitError, ValueError, TypeError) as exc:
                self.error(where, str(exc))
        return channels, time_spec

    def load_design(self) -> dict[str, DesignParam]:
        raw = self.mapping(self.read_yaml("design.yaml"), "design.yaml")
        params: dict[str, DesignParam] = {}
        for name, value in self.mapping(raw.get("parameters", {}), "design.yaml: parameters").items():
            where = f"design.yaml: {name}"
            entry = value if isinstance(value, dict) else {"value": value}
            unknown = set(entry) - {"value", "tolerance", "source", "note"}
            if unknown:
                self.error(where, f"unknown keys: {', '.join(sorted(unknown))}")
            if "value" not in entry:
                self.error(where, "needs a 'value'")
                continue
            try:
                quantity = parse_quantity(entry["value"])
                tolerance = EXACT
                if "tolerance" in entry:
                    tolerance = parse_tolerance(entry["tolerance"], quantity.dimension)
                params[str(name)] = DesignParam(str(name), quantity, tolerance, str(entry.get("source", "")))
            except UnitError as exc:
                self.error(where, str(exc))
        return params

    def load_requirements(self) -> dict[str, Requirement]:
        requirements: dict[str, Requirement] = {}
        for entry in self.items(self.read_yaml("requirements.yaml"), "requirements", "requirements.yaml"):
            rid = str(entry["id"])
            where = f"requirements.yaml: {rid}"
            if rid in requirements:
                self.error(where, "duplicate requirement id")
                continue
            unknown = set(entry) - {"id", "title", "text", "parent", "method", "condition"}
            if unknown:
                self.error(where, f"unknown keys: {', '.join(sorted(unknown))}")
            method = entry.get("method")
            condition = None
            if method is not None and method not in METHODS:
                self.error(where, f"method must be one of {', '.join(METHODS)}, got '{method}'")
                method = None
            if entry.get("condition") is not None:
                try:
                    condition = parse_condition(entry["condition"])
                except (ConditionError, UnitError) as exc:
                    self.error(where, f"invalid condition: {exc}")
                if method is None and condition is not None:
                    self.error(where, "has a condition but no 'method' (test, analysis or inspection)")
            elif method is not None:
                self.error(where, f"method is '{method}' but there is no 'condition' to check")
            if condition is not None and method is not None:
                if method == "test" and not condition.is_telemetry:
                    self.error(where, "test requirements must measure a channel, not value(...)")
                if method in ("analysis", "inspection") and condition.is_telemetry:
                    self.error(where, f"{method} requirements must use value(...), not channel data")
            requirements[rid] = Requirement(
                id=rid,
                title=str(entry.get("title") or rid),
                text=str(entry.get("text") or "").strip(),
                parent=None if entry.get("parent") is None else str(entry["parent"]),
                method=method,
                condition=condition,
            )
        return requirements

    def load_test_cases(self) -> dict[str, TestCase]:
        cases: dict[str, TestCase] = {}
        for entry in self.items(self.read_yaml("test_cases.yaml"), "test_cases", "test_cases.yaml"):
            tid = str(entry["id"])
            where = f"test_cases.yaml: {tid}"
            if tid in cases:
                self.error(where, "duplicate test case id")
                continue
            allowed = {"id", "title", "description", "verifies", "configuration", "events", "windows"}
            unknown = set(entry) - allowed
            if unknown:
                self.error(where, f"unknown keys: {', '.join(sorted(unknown))}")
            events: dict[str, EventSpec] = {}
            windows: dict[str, WindowSpec] = {}
            for name, spec in self.mapping(entry.get("events"), f"{where}: events").items():
                try:
                    events[str(name)] = parse_event(str(name), spec)
                except WindowError as exc:
                    self.error(where, str(exc))
            for name, spec in self.mapping(entry.get("windows"), f"{where}: windows").items():
                if str(name) in ("start", "end"):
                    self.error(where, f"'{name}' is reserved and cannot be a window name")
                    continue
                try:
                    windows[str(name)] = parse_window(str(name), spec)
                except WindowError as exc:
                    self.error(where, str(exc))
            cases[tid] = TestCase(
                id=tid,
                title=str(entry.get("title") or tid),
                verifies=tuple(str(v) for v in entry.get("verifies", []) or []),
                configuration=tuple(str(c) for c in entry.get("configuration", []) or []),
                events=events,
                windows=windows,
                description=str(entry.get("description") or "").strip(),
            )
        return cases

    def load_runs(self, design: dict[str, DesignParam]) -> dict[str, Run]:
        runs: dict[str, Run] = {}
        runs_dir = self.root / "runs"
        if not runs_dir.is_dir():
            return runs
        for meta_path in sorted(runs_dir.glob("*.y*ml")):
            relative = f"runs/{meta_path.name}"
            data = self.mapping(self.read_yaml(relative), relative)
            if not data:
                continue
            rid = str(data.get("id") or meta_path.stem)
            where = f"{relative} ({rid})"
            if rid in runs:
                self.error(where, "duplicate run id")
                continue
            allowed = {"id", "test_case", "date", "data", "article", "configuration", "notes", "void"}
            unknown = set(data) - allowed
            if unknown:
                self.error(where, f"unknown keys: {', '.join(sorted(unknown))}")
            missing = [k for k in ("test_case", "date", "data") if not data.get(k)]
            if missing:
                self.error(where, f"missing {', '.join(missing)}")
                continue
            data_path = (runs_dir / str(data["data"])).resolve()
            if not data_path.is_file():
                self.error(where, f"data file not found: {data['data']}")
            configuration: dict[str, Quantity] = {}
            for name, value in self.mapping(data.get("configuration"), f"{where}: configuration").items():
                try:
                    q = parse_quantity(value)
                except UnitError as exc:
                    self.error(where, f"configuration '{name}': {exc}")
                    continue
                param = design.get(str(name))
                if param and q.dimension != param.quantity.dimension:
                    self.error(
                        where,
                        f"configuration '{name}' is a {q.dimension}, design parameter is a {param.quantity.dimension}",
                    )
                    continue
                configuration[str(name)] = q
            runs[rid] = Run(
                id=rid,
                test_case=str(data["test_case"]),
                date=_date_text(data["date"]),
                data=data_path,
                article=str(data.get("article", "")),
                configuration=configuration,
                notes=str(data.get("notes") or "").strip(),
                void=None if not data.get("void") else str(data["void"]),
                meta_path=meta_path,
            )
        return runs

    # ---------------------------------------------------------------- cross references

    def cross_check(self, p: Project) -> None:
        self.check_requirement_tree(p)
        self.check_conditions(p)
        self.check_test_cases(p)
        self.check_runs(p)
        if p.model:
            for name, dimension in p.model.inputs.items():
                if name not in p.design:
                    self.error("design.yaml", f"model '{p.model.name}' needs parameter '{name}'")
                elif p.design[name].quantity.dimension != dimension:
                    actual = p.design[name].quantity.dimension
                    self.error(f"design.yaml: {name}", f"model expects a {dimension}, got a {actual}")
            for name in p.model.outputs:
                if name in p.design:
                    self.error(f"design.yaml: {name}", f"name clashes with model output '{name}'")

    def check_requirement_tree(self, p: Project) -> None:
        for req in p.requirements.values():
            if req.parent is not None and req.parent not in p.requirements:
                self.error(f"requirements.yaml: {req.id}", f"parent '{req.parent}' does not exist")
        for rid in list(p.requirements):
            req = p.requirements[rid]
            seen = {req.id}
            current = req.parent
            while current is not None and current in p.requirements:
                if current in seen:
                    self.error(f"requirements.yaml: {req.id}", "parent links form a cycle")
                    # Break the cycle so later tree walks terminate.
                    p.requirements[req.id] = dataclasses.replace(req, parent=None)
                    break
                seen.add(current)
                current = p.requirements[current].parent

    def check_conditions(self, p: Project) -> None:
        for req in p.requirements.values():
            cond = req.condition
            if cond is None:
                continue
            where = f"requirements.yaml: {req.id}"
            measure = cond.measure
            threshold = cond.threshold
            if isinstance(measure, ValueRef):
                dimension = p.value_dimension(measure.name)
                if dimension is None:
                    self.error(where, f"value({measure.name}): no design parameter or model output by that name")
                elif dimension != threshold.dimension:
                    self.error(
                        where,
                        f"value({measure.name}) is a {dimension}, threshold {threshold} is a {threshold.dimension}",
                    )
                continue
            channel = p.channels.get(measure.channel)
            if channel is None:
                defined = ", ".join(p.channels) or "none"
                self.error(where, f"unknown channel '{measure.channel}' (defined: {defined})")
                continue
            dimension = channel.unit.dimension
            if isinstance(measure, Aggregate) and dimension != threshold.dimension:
                self.error(
                    where,
                    f"'{measure.channel}' is a {dimension}, threshold {threshold} is a {threshold.dimension}",
                )
            if isinstance(measure, Duration) and dimension != measure.threshold.dimension:
                self.error(
                    where,
                    f"'{measure.channel}' is a {dimension}, "
                    f"duration level {measure.threshold} is a {measure.threshold.dimension}",
                )
            if req.method == "test" and not p.test_cases_for(req.id):
                self.warn(where, "no test case verifies this requirement")

    def check_test_cases(self, p: Project) -> None:
        for tc in p.test_cases.values():
            where = f"test_cases.yaml: {tc.id}"
            for rid in tc.verifies:
                req = p.requirements.get(rid)
                if req is None:
                    self.error(where, f"verifies unknown requirement '{rid}'")
                    continue
                if req.method != "test":
                    self.error(where, f"verifies '{rid}', whose method is {req.method or 'not set'} (not test)")
                    continue
                if req.condition and req.condition.window and req.condition.window not in tc.windows:
                    self.error(
                        where,
                        f"'{rid}' is evaluated during '{req.condition.window}', which this test case does not define",
                    )
            for name in tc.configuration:
                if name not in p.design:
                    self.error(where, f"configuration lists unknown design parameter '{name}'")
            for event in tc.events.values():
                if event.channel not in p.channels:
                    self.error(where, f"event '{event.name}' uses unknown channel '{event.channel}'")
                elif event.threshold.dimension != p.channels[event.channel].unit.dimension:
                    self.error(
                        where, f"event '{event.name}' threshold {event.threshold} does not match '{event.channel}'"
                    )
                if event.after is not None and event.after not in tc.events:
                    self.error(where, f"event '{event.name}' is after unknown event '{event.after}'")
            for window in tc.windows.values():
                for ref in window.events:
                    if ref not in tc.events:
                        self.error(where, f"window '{window.name}' refers to unknown event '{ref}'")

    def check_runs(self, p: Project) -> None:
        for run in p.runs.values():
            where = f"runs/{run.meta_path.name if run.meta_path else run.id} ({run.id})"
            tc = p.test_cases.get(run.test_case)
            if tc is None:
                self.error(where, f"unknown test case '{run.test_case}'")
                continue
            missing = [c for c in tc.configuration if c not in run.configuration]
            if missing:
                self.warn(where, f"does not record {', '.join(missing)}; it will need a retest to count")
