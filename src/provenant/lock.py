"""The lock file and change detection.

``provenant verify`` records the verified state in ``provenant.lock``: every
active piece of evidence with its component fingerprints and verdict, every
requirement status, and the design snapshot. Commit it next to the project.

Comparing a fresh evaluation against the lock answers the two questions that
matter after any change: *what evidence is now stale and why* and *which
verdicts flip*. The lock is only rewritten when its content changes, so it does
not churn in version control.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .evaluate import Evaluation, Evidence, RetestItem
from .units import format_si

LOCK_NAME = "provenant.lock"
LOCK_FORMAT = 1

COMPONENT_REASONS = {
    "requirement": "requirement condition changed",
    "test_case": "test case window/event definition changed",
    "data": "run data file changed",
    "channels": "channel mapping or calibration changed",
    "configuration": "run configuration record changed",
    "params": "design parameters changed",
    "model": "analysis model changed",
}


class LockError(Exception):
    pass


@dataclass
class Lock:
    generated_at: str
    tool: str
    design: dict[str, dict[str, str]]
    requirements: dict[str, dict[str, Any]]
    evidence: dict[str, dict[str, Any]]
    model_outputs: dict[str, str]

    def content(self) -> dict[str, Any]:
        return {
            "format": LOCK_FORMAT,
            "design": self.design,
            "requirements": self.requirements,
            "evidence": self.evidence,
            "model_outputs": self.model_outputs,
        }


def build_lock(evaluation: Evaluation) -> Lock:
    project = evaluation.project
    if project.overrides:
        raise LockError("refusing to lock a what-if evaluation (--set overrides are active)")
    design = {
        name: {"value": format_si(p.quantity.si, p.quantity.unit), "si": p.quantity.canonical()}
        for name, p in sorted(project.design.items())
    }
    requirements = {
        rid: {
            "title": result.requirement.title,
            "status": result.status,
            "reason": result.reason,
            "condition": result.requirement.condition.source if result.requirement.condition else None,
        }
        for rid, result in evaluation.results.items()
    }
    evidence = {
        e.key: {
            "requirement": e.requirement_id,
            "source": e.source,
            "test_case": e.test_case_id,
            "verdict": e.verdict,
            "measured": e.measured,
            "margin": None if e.margin is None else round(e.margin, 6),
            "applicable": e.applicable,
            "components": dict(sorted(e.components.items())),
        }
        for e in sorted(evaluation.evidence, key=lambda e: e.key)
        if e.counts
    }
    outputs = {name: str(q) for name, q in sorted(evaluation.model_outputs.items())}
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    return Lock(now, f"provenant {__version__}", design, requirements, evidence, outputs)


def read_lock(root: Path) -> Lock | None:
    path = root / LOCK_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LockError(f"{LOCK_NAME} is not valid JSON ({exc}); re-run 'provenant verify'") from None
    if not isinstance(raw, dict) or raw.get("format") != LOCK_FORMAT:
        found = raw.get("format") if isinstance(raw, dict) else None
        raise LockError(f"{LOCK_NAME} has format {found!r}; this version reads {LOCK_FORMAT}")
    try:
        return Lock(
            generated_at=raw["generated_at"],
            tool=raw.get("tool", ""),
            design=raw["design"],
            requirements=raw["requirements"],
            evidence=raw["evidence"],
            model_outputs=raw.get("model_outputs", {}),
        )
    except KeyError as exc:
        raise LockError(f"{LOCK_NAME} is missing {exc}; re-run 'provenant verify'") from None


def write_lock(root: Path, lock: Lock) -> bool:
    """Write the lock if its content changed. Returns True when the file was written."""
    path = root / LOCK_NAME
    existing = None
    if path.is_file():
        try:
            existing = read_lock(root)
        except LockError:
            existing = None
    if existing is not None and existing.content() == lock.content():
        return False
    payload = {"format": LOCK_FORMAT, "generated_at": lock.generated_at, "tool": lock.tool, **lock.content()}
    tmp = path.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and Windows
    return True


# --------------------------------------------------------------------------- comparison


@dataclass
class StaleEvidence:
    key: str
    requirement_id: str
    source: str
    kind: str  # "reevaluate" | "retest" | "new" | "removed"
    reasons: list[str]
    before_verdict: str | None = None
    after_verdict: str | None = None
    before_measured: str | None = None
    after_measured: str | None = None


@dataclass
class StatusChange:
    requirement_id: str
    title: str
    before: str | None
    after: str | None
    reason: str


@dataclass
class ValueChange:
    name: str
    before: str | None
    after: str | None


@dataclass
class ChangeReport:
    has_lock: bool
    locked_at: str | None
    design_changes: list[ValueChange] = field(default_factory=list)
    condition_changes: list[ValueChange] = field(default_factory=list)
    output_changes: list[ValueChange] = field(default_factory=list)
    stale: list[StaleEvidence] = field(default_factory=list)
    status_changes: list[StatusChange] = field(default_factory=list)
    retest_queue: list[RetestItem] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.has_lock and not self.stale and not self.status_changes


def _evidence_change(
    before: dict[str, Any] | None, after: Evidence | None, key: str, superseded: dict[str, str]
) -> StaleEvidence | None:
    if before is None and after is not None:
        return StaleEvidence(
            key, after.requirement_id, after.source, "new", ["no locked verdict yet"],
            None, after.verdict, None, after.measured,
        )  # fmt: skip
    if before is not None and after is None:
        reason = f"superseded by {superseded[key]}" if key in superseded else "source no longer exists"
        return StaleEvidence(
            key, before["requirement"], before["source"], "removed", [reason],
            before["verdict"], None, before.get("measured"), None,
        )  # fmt: skip
    assert before is not None and after is not None
    reasons = [
        COMPONENT_REASONS.get(name, f"{name} changed")
        for name in sorted(set(before["components"]) | set(after.components))
        if before["components"].get(name) != after.components.get(name)
    ]
    args = (before["verdict"], after.verdict, before.get("measured"), after.measured)
    if reasons:
        return StaleEvidence(key, after.requirement_id, after.source, "reevaluate", reasons, *args)
    if before.get("applicable", True) and not after.applicable:
        detail = [
            f"design changed since this run: {d.parameter} tested at {d.recorded}, now {d.current}"
            for d in after.config_diffs
        ]
        return StaleEvidence(key, after.requirement_id, after.source, "retest", detail, *args)
    if not before.get("applicable", True) and after.applicable:
        restored = ["design is back within the tested configuration"]
        return StaleEvidence(key, after.requirement_id, after.source, "reevaluate", restored, *args)
    return None


def compare(lock: Lock | None, evaluation: Evaluation) -> ChangeReport:
    report = ChangeReport(
        has_lock=lock is not None,
        locked_at=lock.generated_at if lock else None,
        retest_queue=evaluation.retest_queue,
    )
    project = evaluation.project
    current = {e.key: e for e in evaluation.evidence if e.counts}
    superseded = {e.key: e.superseded_by for e in evaluation.evidence if e.superseded_by}

    if lock is None:
        for key, e in sorted(current.items()):
            report.stale.append(
                StaleEvidence(
                    key, e.requirement_id, e.source, "new", ["never verified"], None, e.verdict, None, e.measured
                )
            )
        return report

    # Inputs that changed, for the human-readable summary.
    design_now = {
        n: (format_si(p.quantity.si, p.quantity.unit), p.quantity.canonical()) for n, p in project.design.items()
    }
    for name in sorted(set(lock.design) | set(design_now)):
        before = lock.design.get(name)
        after = design_now.get(name)
        if (before or {}).get("si") != (after[1] if after else None):
            report.design_changes.append(
                ValueChange(name, before["value"] if before else None, after[0] if after else None)
            )
    for rid in sorted(set(lock.requirements) | set(project.requirements)):
        before_cond = lock.requirements.get(rid, {}).get("condition")
        req = project.requirements.get(rid)
        after_cond = req.condition.source if req and req.condition else None
        if before_cond != after_cond:
            report.condition_changes.append(ValueChange(rid, before_cond, after_cond))
    outputs_now = {n: str(q) for n, q in evaluation.model_outputs.items()}
    for name in sorted(set(lock.model_outputs) | set(outputs_now)):
        if lock.model_outputs.get(name) != outputs_now.get(name):
            report.output_changes.append(ValueChange(name, lock.model_outputs.get(name), outputs_now.get(name)))

    for key in sorted(set(lock.evidence) | set(current)):
        change = _evidence_change(lock.evidence.get(key), current.get(key), key, superseded)
        if change:
            report.stale.append(change)

    ordered_ids = list(evaluation.results) + sorted(set(lock.requirements) - set(evaluation.results))
    for rid in ordered_ids:
        status_before: str | None = lock.requirements.get(rid, {}).get("status")
        result = evaluation.results.get(rid)
        status_after = result.status if result else None
        if status_before != status_after:
            title = result.requirement.title if result else lock.requirements[rid].get("title", rid)
            reason = result.reason if result else "removed"
            report.status_changes.append(StatusChange(rid, title, status_before, status_after, reason))
    return report
