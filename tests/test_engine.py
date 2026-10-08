"""End-to-end behaviour of evaluation, staleness and impact on the demo project."""

import pytest

from conftest import edit
from provenant.evaluate import ERROR, FAILED, RETEST, UNVERIFIED, VERIFIED, evaluate
from provenant.lock import LockError, build_lock, compare, read_lock, write_lock
from provenant.project import Project
from provenant.units import parse_quantity


def baseline(demo):
    evaluation = evaluate(Project.load(demo))
    write_lock(demo, build_lock(evaluation))
    return evaluation


def changes(demo, **overrides):
    project = Project.load(demo)
    if overrides:
        project = project.with_overrides({k: parse_quantity(v) for k, v in overrides.items()})
    evaluation = evaluate(project)
    return evaluation, compare(read_lock(demo), evaluation)


def test_baseline_statuses(demo):
    ev = evaluate(Project.load(demo))
    status = {rid: r.status for rid, r in ev.results.items()}
    assert status["PRP-010"] == VERIFIED
    assert status["PRP-044"] == VERIFIED
    assert status["PRP-060"] == UNVERIFIED
    assert status["SYS-001"] == UNVERIFIED  # rolls up the unverified child
    assert status["PRP-001"] == VERIFIED
    assert ev.coverage == pytest.approx(0.9)
    assert [i.test_case.id for i in ev.retest_queue] == ["TC-RS-001"]


def test_latest_run_supersedes_and_void_runs_are_ignored(demo):
    ev = evaluate(Project.load(demo))
    by_key = ev.evidence_by_key()
    assert by_key["TCS-020@RUN-0001"].superseded_by == "RUN-0003"
    assert by_key["TCS-020@RUN-0003"].superseded_by is None
    assert not any(e.source == "RUN-0004" for e in ev.evidence)  # void: facility abort


def test_alias_resolves_renamed_column(demo):
    ev = evaluate(Project.load(demo))
    assert ev.evidence_by_key()["PRP-031@RUN-0003"].provenance.column == "PT101"
    assert ev.evidence_by_key()["PRP-031@RUN-0002"].provenance.column == "PT-101"


def test_provenance_is_complete(demo):
    e = evaluate(Project.load(demo)).evidence_by_key()["TCS-020@RUN-0003"]
    pv = e.provenance
    assert pv.window == "steady_state"
    assert pv.t0 == pytest.approx(pv.events["ignition"] + 2.0)
    assert pv.t1 == pytest.approx(pv.events["shutdown"] - 1.0)
    assert pv.t0 <= pv.t_at <= pv.t1
    assert pv.missing > 0  # the TC-205 dropout is accounted for, not hidden
    assert len(pv.data_sha256) == 64
    assert e.margin == pytest.approx((900 - e.measured_si) / 900)


def test_clean_after_verify(demo):
    baseline(demo)
    _, report = changes(demo)
    assert report.clean


def test_lock_not_rewritten_when_unchanged(demo):
    ev = baseline(demo)
    assert write_lock(demo, build_lock(ev)) is False


def test_corrupt_lock_is_reported(demo):
    (demo / "provenant.lock").write_text("{not json", encoding="utf-8")
    with pytest.raises(LockError, match="not valid JSON"):
        read_lock(demo)
    (demo / "provenant.lock").write_text('{"format": 99}', encoding="utf-8")
    with pytest.raises(LockError, match="format 99"):
        read_lock(demo)
    (demo / "provenant.lock").write_text('{"format": 1}', encoding="utf-8")
    with pytest.raises(LockError, match="missing"):
        read_lock(demo)


def test_design_change_flips_analysis_and_invalidates_tests(demo):
    baseline(demo)
    edit(demo / "design.yaml", "value: 120 bar", "value: 100 bar")
    ev, report = changes(demo)
    status = {rid: r.status for rid, r in ev.results.items()}
    assert status["PRP-011"] == FAILED  # predicted thrust drops below 20 kN
    assert status["PRP-044"] == VERIFIED  # ideal Isp does not depend on chamber pressure
    assert status["PRP-010"] == RETEST
    assert status["TCS-020"] == RETEST
    assert status["STR-012"] == VERIFIED  # mass is not sensitive to chamber pressure
    retest = {s.key for s in report.stale if s.kind == "retest"}
    assert "PRP-010@RUN-0003" in retest and "PRP-033@RUN-0002" in retest
    assert {i.test_case.id for i in ev.retest_queue} == {"TC-HF-001", "TC-HF-002", "TC-RS-001"}
    assert [c.name for c in report.design_changes] == ["chamber_pressure_nominal"]


def test_reverting_design_restores_verdicts(demo):
    edit(demo / "design.yaml", "value: 120 bar", "value: 100 bar")
    baseline(demo)
    edit(demo / "design.yaml", "value: 100 bar", "value: 120 bar")
    ev, report = changes(demo)
    assert ev.results["PRP-010"].status == VERIFIED
    assert any("back within the tested configuration" in r for s in report.stale for r in s.reasons)


def test_change_within_tolerance_needs_no_retest(demo):
    baseline(demo)
    edit(demo / "design.yaml", "value: 120 bar", "value: 121 bar")  # tolerance is 2 %
    ev, report = changes(demo)
    assert not any(s.kind == "retest" for s in report.stale)
    assert ev.results["PRP-010"].status == VERIFIED


def test_insensitive_parameter_does_not_invalidate_test(demo):
    baseline(demo)
    edit(demo / "design.yaml", "value: 76.5 kg", "value: 99 kg")
    ev, report = changes(demo)
    assert ev.results["STR-012"].status == FAILED
    assert {s.requirement_id for s in report.stale} == {"STR-012"}


def test_requirement_change_is_reevaluated_not_retested(demo):
    baseline(demo)
    edit(demo / "requirements.yaml", "<= 900 K", "<= 850 K")
    ev, report = changes(demo)
    assert ev.results["TCS-020"].status == FAILED
    stale = [s for s in report.stale if s.requirement_id == "TCS-020"]
    assert stale[0].kind == "reevaluate"
    assert stale[0].reasons == ["requirement condition changed"]
    assert [i.test_case.id for i in ev.retest_queue] == ["TC-RS-001"]


def test_cosmetic_condition_edit_is_not_a_change(demo):
    baseline(demo)
    edit(demo / "requirements.yaml", "max(chamber_pressure) <= 150 bar", "max( chamber_pressure )  <=  15 MPa")
    _, report = changes(demo)
    assert not [s for s in report.stale if s.requirement_id == "PRP-031"]


def test_calibration_change_stales_only_dependent_evidence(demo):
    baseline(demo)
    edit(demo / "channels.yaml", "    unit: lbf\n", "    unit: lbf\n    scale: 1.02\n")
    _, report = changes(demo)
    assert {s.key for s in report.stale} == {"PRP-010@RUN-0003"}
    assert report.stale[0].reasons == ["channel mapping or calibration changed"]


def test_event_definition_change_stales_windowed_evidence(demo):
    baseline(demo)
    edit(demo / "test_cases.yaml", "steady_state: {start: ignition + 2 s", "steady_state: {start: ignition + 3 s")
    _, report = changes(demo)
    keys = {s.key for s in report.stale}
    assert {"PRP-010@RUN-0003", "TCS-020@RUN-0003"} <= keys
    assert "PRP-031@RUN-0003" not in keys  # whole-record check does not use the window
    assert "PRP-050@RUN-0003" not in keys


def test_data_file_change_detected(demo):
    baseline(demo)
    path = demo / "runs" / "RUN-0002.csv"
    path.write_text(path.read_text(encoding="utf-8"))  # identical content: not a change
    _, report = changes(demo)
    assert report.clean
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[200] = lines[200].rsplit(",", 1)[0] + ",9.9999"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, report = changes(demo)
    assert {s.key for s in report.stale} == {"PRP-031@RUN-0002", "PRP-033@RUN-0002"}
    assert all(s.reasons == ["run data file changed"] for s in report.stale)


def test_new_run_supersedes_previous(demo):
    baseline(demo)
    for suffix in ("csv", "yaml"):
        src = (demo / "runs" / f"RUN-0003.{suffix}").read_text(encoding="utf-8")
        src = src.replace("RUN-0003", "RUN-0005").replace("2026-08-21", "2026-09-30")
        (demo / "runs" / f"RUN-0005.{suffix}").write_text(src, encoding="utf-8")
    _, report = changes(demo)
    kinds = {(s.key, s.kind) for s in report.stale}
    assert ("TCS-020@RUN-0005", "new") in kinds
    assert ("TCS-020@RUN-0003", "removed") in kinds
    removed = next(s for s in report.stale if s.key == "TCS-020@RUN-0003")
    assert removed.reasons == ["superseded by RUN-0005"]


def test_missing_event_is_an_error_with_context(demo):
    edit(demo / "test_cases.yaml", "falls_below: 10 bar, after: ignition", "falls_below: -5 bar, after: ignition")
    ev = evaluate(Project.load(demo))
    assert ev.results["PRP-010"].status == ERROR
    assert "event 'shutdown'" in ev.results["PRP-010"].reason
    assert "not observed in RUN-0003" in ev.results["PRP-010"].reason


def test_corrupt_data_file_is_an_error_not_a_crash(demo):
    (demo / "runs" / "RUN-0002.csv").write_text("time_s,PT-101\n0,1\n0,2\n", encoding="utf-8")
    ev = evaluate(Project.load(demo))
    assert ev.results["PRP-033"].status == ERROR
    assert "strictly increasing" in ev.results["PRP-033"].reason


def test_model_failure_is_reported_not_raised(demo):
    edit(demo / "design.yaml", "expansion_ratio: {value: 30", "expansion_ratio: {value: 0.5")
    ev = evaluate(Project.load(demo))
    assert "expansion_ratio must be at least 1" in (ev.model_error or "")
    assert ev.results["PRP-044"].status == ERROR
    assert ev.results["STR-012"].status == VERIFIED


def test_what_if_does_not_touch_files(demo):
    baseline(demo)
    before = (demo / "provenant.lock").read_text(encoding="utf-8")
    ev, _ = changes(demo, chamber_pressure_nominal="100 bar")
    assert ev.results["PRP-011"].status == FAILED
    assert (demo / "provenant.lock").read_text(encoding="utf-8") == before
    with pytest.raises(LockError, match="what-if"):
        build_lock(ev)


def test_no_lock_reports_everything_as_new(demo):
    ev = evaluate(Project.load(demo))
    report = compare(None, ev)
    assert not report.clean
    assert all(s.kind == "new" for s in report.stale)
