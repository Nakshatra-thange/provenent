"""The evaluation engine.

For every requirement it produces *evidence* — one verdict per source (a test
run, or the current design for analysis/inspection) — and rolls evidence up to
a requirement status.

Each piece of evidence carries a set of *component fingerprints*: hashes of
exactly the inputs that verdict depended on (the canonical condition, the
window/event definitions it used, the run's data file, the channel mappings it
read, the design parameters it consumed). Comparing those against the lock file
tells us precisely which evidence went stale and why, without rerunning
anything — the same idea as a build system's content-addressed cache.

Separately, test evidence is checked for *applicability*: the hardware
configuration recorded with the run must match the current design for every
parameter the test case declares itself sensitive to. A mismatch cannot be fixed
by recomputing; the test has to be re-run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .expr import Aggregate, Condition, Duration, ValueRef
from .physics import ModelError
from .project import Project, Requirement, Run, TestCase
from .telemetry import Recording, TelemetryError
from .units import Quantity, Unit, format_si, get_unit, within_tolerance
from .windows import EventResolver, WindowError, duration, events_needed, reduce

VERIFIED = "verified"
FAILED = "failed"
ERROR = "error"
RETEST = "retest"
UNVERIFIED = "unverified"

STATUS_ORDER = (FAILED, ERROR, RETEST, UNVERIFIED, VERIFIED)  # most to least severe

PASS = "pass"
FAIL = "fail"


def digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


@dataclass(frozen=True)
class ConfigDiff:
    parameter: str
    recorded: str
    current: str


@dataclass
class Provenance:
    """Everything needed to reproduce and inspect a verdict."""

    channel: str | None = None
    column: str | None = None
    raw_unit: str | None = None
    window: str | None = None
    window_definition: str | None = None
    t0: float | None = None
    t1: float | None = None
    t_at: float | None = None
    samples: int | None = None
    missing: int | None = None
    data_file: str | None = None
    data_sha256: str | None = None
    events: dict[str, float] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)
    # Downsampled (time, value-in-display-unit) points for plotting; optional.
    trace: list[tuple[float, float | None]] = field(default_factory=list)
    trace_unit: str | None = None


@dataclass
class Evidence:
    key: str
    requirement_id: str
    method: str
    source: str  # run id, or "design"
    verdict: str  # pass | fail | error
    components: dict[str, str]
    measured_si: float | None = None
    measured: str | None = None
    threshold: str | None = None
    margin: float | None = None
    message: str | None = None
    test_case_id: str | None = None
    run: Run | None = None
    applicable: bool = True
    config_diffs: list[ConfigDiff] = field(default_factory=list)
    superseded_by: str | None = None
    provenance: Provenance = field(default_factory=Provenance)

    @property
    def fingerprint(self) -> str:
        return digest(self.components)

    @property
    def counts(self) -> bool:
        """Whether this evidence currently speaks for the requirement."""
        return self.superseded_by is None


@dataclass
class RequirementResult:
    requirement: Requirement
    status: str
    reason: str
    evidence: list[Evidence]

    @property
    def active_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence if e.counts]


@dataclass
class RetestItem:
    test_case: TestCase
    reasons: list[str]
    requirements: list[str]
    last_run: Run | None


@dataclass
class Evaluation:
    project: Project
    results: dict[str, RequirementResult]
    evidence: list[Evidence]
    model_outputs: dict[str, Quantity]
    model_error: str | None
    retest_queue: list[RetestItem]

    @property
    def leaves(self) -> list[RequirementResult]:
        return [r for r in self.results.values() if r.requirement.condition is not None]

    def counts(self) -> dict[str, int]:
        totals = {status: 0 for status in STATUS_ORDER}
        for result in self.leaves:
            totals[result.status] += 1
        return totals

    @property
    def coverage(self) -> float:
        leaves = self.leaves
        if not leaves:
            return 0.0
        return sum(1 for r in leaves if r.status == VERIFIED) / len(leaves)

    def evidence_by_key(self) -> dict[str, Evidence]:
        return {e.key: e for e in self.evidence}


# --------------------------------------------------------------------------- helpers


def _downsample(t: list[float], v: list[float | None], limit: int = 400) -> list[tuple[float, float | None]]:
    """Min/max bucketing: keeps peaks that naive decimation would drop."""
    n = len(t)
    if n <= limit:
        return list(zip(t, v, strict=True))
    buckets = limit // 2
    out: list[tuple[float, float | None]] = []
    for b in range(buckets):
        lo, hi = b * n // buckets, (b + 1) * n // buckets
        chunk: list[tuple[float, float]] = []
        for i in range(lo, hi):
            value = v[i]
            if value is not None:
                chunk.append((t[i], value))
        if not chunk:
            out.append((t[lo], None))
            continue
        low = min(chunk, key=lambda p: p[1])
        high = max(chunk, key=lambda p: p[1])
        out.extend(sorted({low, high}))
    return out


def _channel_payload(project: Project, names: set[str]) -> dict[str, object]:
    return {
        "time": project.time.fingerprint_payload(),
        "channels": {n: project.channels[n].fingerprint_payload() for n in sorted(names) if n in project.channels},
    }


def _config_diffs(project: Project, tc: TestCase, run: Run) -> list[ConfigDiff]:
    diffs: list[ConfigDiff] = []
    for name in tc.configuration:
        param = project.design.get(name)
        if param is None:
            continue
        recorded = run.configuration.get(name)
        current = format_si(param.quantity.si, param.quantity.unit)
        if recorded is None:
            diffs.append(ConfigDiff(name, "not recorded", current))
        elif not within_tolerance(recorded.si, param.quantity.si, param.tolerance):
            diffs.append(ConfigDiff(name, format_si(recorded.si, param.quantity.unit), current))
    return diffs


class _Engine:
    def __init__(self, project: Project, keep_traces: bool):
        self.p = project
        self.keep_traces = keep_traces
        self._recordings: dict[Path, Recording | TelemetryError] = {}
        self.model_outputs: dict[str, Quantity] = {}
        self.model_error: str | None = None
        self._run_model()

    def _run_model(self) -> None:
        model = self.p.model
        if model is None:
            return
        try:
            inputs = {name: self.p.design[name].quantity.si for name in model.inputs}
            raw = model.compute(inputs)
        except (ModelError, KeyError, ValueError, ZeroDivisionError, OverflowError) as exc:
            self.model_error = f"model '{model.name}' failed: {exc}"
            return
        for name, (_dimension, unit) in model.outputs.items():
            self.model_outputs[name] = Quantity(raw[name], get_unit(unit))

    def recording(self, run: Run) -> Recording:
        cached = self._recordings.get(run.data)
        if cached is None:
            try:
                cached = Recording.load(run.data, self.p.time)
            except TelemetryError as exc:
                cached = exc
            self._recordings[run.data] = cached
        if isinstance(cached, TelemetryError):
            raise cached
        return cached

    # ------------------------------------------------------------------ design-based

    def design_evidence(self, req: Requirement) -> Evidence:
        cond = req.condition
        assert cond is not None and isinstance(cond.measure, ValueRef)
        name = cond.measure.name
        model = self.p.model
        uses_model = model is not None and name in model.outputs
        param_names = sorted(model.inputs) if uses_model and model else [name]
        params = {n: self.p.design[n].quantity.canonical() for n in param_names if n in self.p.design}
        components = {"requirement": digest(cond.canonical()), "params": digest(params)}
        if uses_model and model:
            components["model"] = digest([model.name, model.version])
        provenance = Provenance(
            parameters={
                n: format_si(self.p.design[n].quantity.si, self.p.design[n].quantity.unit)
                for n in param_names
                if n in self.p.design
            }
        )
        evidence = Evidence(
            key=f"{req.id}@design",
            requirement_id=req.id,
            method=req.method or "analysis",
            source="design",
            verdict=ERROR,
            components=components,
            threshold=str(cond.threshold),
            provenance=provenance,
        )
        if uses_model:
            if self.model_error:
                evidence.message = self.model_error
                return evidence
            quantity = self.model_outputs[name]
        else:
            quantity = self.p.design[name].quantity
        self._judge(evidence, quantity.si, cond)
        return evidence

    # ------------------------------------------------------------------ test-based

    def run_evidence(self, req: Requirement, tc: TestCase, run: Run) -> Evidence:
        cond = req.condition
        assert cond is not None and not isinstance(cond.measure, ValueRef)
        window = tc.windows.get(cond.window) if cond.window else None
        needed_events = events_needed([window] if window else [], tc.events)
        channels_used = set(cond.channels) | {tc.events[e].channel for e in needed_events if e in tc.events}
        components = {
            "requirement": digest(cond.canonical()),
            "test_case": digest(
                {
                    "window": window.fingerprint_payload() if window else None,
                    "events": {e: tc.events[e].fingerprint_payload() for e in needed_events if e in tc.events},
                }
            ),
            "data": file_digest(run.data),
            "channels": digest(_channel_payload(self.p, channels_used)),
            "configuration": digest({k: q.canonical() for k, q in sorted(run.configuration.items())}),
        }
        diffs = _config_diffs(self.p, tc, run)
        channel_name = next(iter(cond.channels))
        spec = self.p.channels[channel_name]
        evidence = Evidence(
            key=f"{req.id}@{run.id}",
            requirement_id=req.id,
            method="test",
            source=run.id,
            verdict=ERROR,
            components=components,
            threshold=str(cond.threshold),
            test_case_id=tc.id,
            run=run,
            applicable=not diffs,
            config_diffs=diffs,
            provenance=Provenance(
                channel=channel_name,
                raw_unit=spec.unit.symbol,
                window=cond.window,
                window_definition=window.describe() if window else "entire record",
                data_file=self._relative(run.data),
            ),
        )
        try:
            recording = self.recording(run)
            evidence.provenance.data_sha256 = recording.sha256
            resolver = EventResolver(recording, self.p.channels, tc.events, run.id)
            t0, t1 = resolver.window(window)
            evidence.provenance.t0, evidence.provenance.t1 = t0, t1
            evidence.provenance.events = {e: resolver.time_of(e) for e in needed_events}
            signal = resolver.signal(channel_name)
            evidence.provenance.column = signal.column
            measure = cond.measure
            if isinstance(measure, Aggregate):
                reduction = reduce(signal, measure.func, t0, t1)
            else:
                assert isinstance(measure, Duration)
                reduction = duration(signal, measure.op, measure.threshold.si, t0, t1)
            display_unit = cond.threshold.unit
            evidence.provenance.t_at = reduction.t_at
            evidence.provenance.samples = reduction.samples
            evidence.provenance.missing = reduction.missing
            if self.keep_traces:
                plot_unit = spec.unit if isinstance(measure, Duration) else display_unit
                evidence.provenance.trace = [
                    (t, None if v is None else plot_unit.from_si(v)) for t, v in _downsample(signal.t, signal.v)
                ]
                evidence.provenance.trace_unit = plot_unit.symbol
            self._judge(evidence, reduction.value, cond, display_unit)
        except (TelemetryError, WindowError) as exc:
            evidence.verdict = ERROR
            evidence.message = str(exc)
        return evidence

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.p.root.resolve()))
        except ValueError:
            return str(path)

    @staticmethod
    def _judge(evidence: Evidence, measured_si: float, cond: Condition, display_unit: Unit | None = None) -> None:
        unit = display_unit or cond.threshold.unit
        evidence.measured_si = measured_si
        evidence.measured = format_si(measured_si, unit)
        evidence.margin = cond.margin(measured_si)
        evidence.verdict = PASS if cond.holds(measured_si) else FAIL

    # ------------------------------------------------------------------ roll-up

    def evaluate(self) -> Evaluation:
        p = self.p
        all_evidence: list[Evidence] = []
        per_requirement: dict[str, list[Evidence]] = {rid: [] for rid in p.requirements}
        unrun: dict[str, list[str]] = {}

        for req in p.requirements.values():
            cond = req.condition
            if cond is None:
                continue
            if not cond.is_telemetry:
                per_requirement[req.id].append(self.design_evidence(req))
                continue
            for tc in p.test_cases_for(req.id):
                runs = [r for r in p.runs_for(tc.id) if not r.void]
                if not runs:
                    unrun.setdefault(req.id, []).append(tc.id)
                    continue
                latest = runs[-1]
                for run in runs:
                    evidence = self.run_evidence(req, tc, run)
                    if run is not latest:
                        evidence.superseded_by = latest.id
                    per_requirement[req.id].append(evidence)

        for items in per_requirement.values():
            all_evidence.extend(items)

        results: dict[str, RequirementResult] = {}
        for req in p.tree_order():
            if req.condition is not None:
                status, reason = self._leaf_status(req, per_requirement[req.id], unrun.get(req.id, []))
                results[req.id] = RequirementResult(req, status, reason, per_requirement[req.id])
        # Parents roll up from children, deepest first.
        for req in sorted(p.requirements.values(), key=lambda r: -p.depth(r.id)):
            if req.condition is None:
                children = [results[c.id] for c in p.children(req.id) if c.id in results]
                status, reason = self._rollup(children)
                results[req.id] = RequirementResult(req, status, reason, [])
        ordered = {req.id: results[req.id] for req in p.tree_order()}

        return Evaluation(
            project=p,
            results=ordered,
            evidence=all_evidence,
            model_outputs=self.model_outputs,
            model_error=self.model_error,
            retest_queue=self._retest_queue(),
        )

    def _leaf_status(self, req: Requirement, evidence: list[Evidence], unrun: list[str]) -> tuple[str, str]:
        active = [e for e in evidence if e.counts]
        if not active and not unrun:
            if req.method == "test" and not self.p.test_cases_for(req.id):
                return UNVERIFIED, "no test case verifies this requirement"
            return UNVERIFIED, "no evidence"
        applicable = [e for e in active if e.applicable]
        failing = [e for e in applicable if e.verdict == FAIL]
        if failing:
            worst = failing[0]
            return FAILED, f"{worst.measured} vs {worst.threshold} ({worst.source})"
        errors = [e for e in applicable if e.verdict == ERROR]
        if errors:
            return ERROR, f"{errors[0].source}: {errors[0].message}"
        stale = [e for e in active if not e.applicable]
        if stale:
            params = sorted({d.parameter for e in stale for d in e.config_diffs})
            runs = ", ".join(sorted({e.source for e in stale}))
            return RETEST, f"{runs} tested a different configuration ({', '.join(params)})"
        if unrun:
            return UNVERIFIED, f"not yet run: {', '.join(unrun)}"
        if req.method == "test":
            sources = ", ".join(e.source for e in active)
            return VERIFIED, f"passed in {sources}"
        return VERIFIED, f"{req.method} of current design"

    @staticmethod
    def _rollup(children: list[RequirementResult]) -> tuple[str, str]:
        if not children:
            return UNVERIFIED, "no child requirements and no condition"
        for status in STATUS_ORDER:
            matching = [c.requirement.id for c in children if c.status == status]
            if matching and status != VERIFIED:
                return status, f"{len(matching)} of {len(children)} children {status}: {', '.join(matching)}"
        return VERIFIED, f"all {len(children)} children verified"

    def _retest_queue(self) -> list[RetestItem]:
        queue: list[RetestItem] = []
        for tc in self.p.test_cases.values():
            if not any(r in self.p.requirements for r in tc.verifies):
                continue
            runs = [r for r in self.p.runs_for(tc.id) if not r.void]
            if not runs:
                queue.append(RetestItem(tc, ["never run"], list(tc.verifies), None))
                continue
            latest = runs[-1]
            diffs = _config_diffs(self.p, tc, latest)
            if diffs:
                reasons = [f"{d.parameter}: tested at {d.recorded}, design is now {d.current}" for d in diffs]
                queue.append(RetestItem(tc, reasons, list(tc.verifies), latest))
        return queue


def evaluate(project: Project, keep_traces: bool = False) -> Evaluation:
    return _Engine(project, keep_traces).evaluate()
