import pytest

from conftest import edit
from provenant.project import Project, ProjectError, find_project_root
from provenant.units import parse_quantity


def errors(path):
    with pytest.raises(ProjectError) as info:
        Project.load(path)
    return [f"{i.where}: {i.message}" for i in info.value.issues if i.severity == "error"]


def test_demo_loads_cleanly(demo):
    project = Project.load(demo)
    assert project.warnings == []
    assert len(project.requirements) == 13
    assert [r.id for r in project.roots()] == ["SYS-001"]
    assert project.depth("PRP-010") == 2
    assert [r.id for r in project.runs_for("TC-HF-001")] == ["RUN-0001", "RUN-0003", "RUN-0004"]


def test_find_project_root(demo):
    assert find_project_root(demo / "runs") == demo.resolve()
    with pytest.raises(ProjectError, match=r"no project\.yaml"):
        find_project_root("/")


def test_collects_all_errors_at_once(demo):
    edit(demo / "requirements.yaml", "parent: PRP-001\n    title: Measured", "parent: NOPE\n    title: Measured")
    edit(demo / "requirements.yaml", "mean(thrust) >= 20 kN", "mean(thrust) >= 20 bar")
    edit(demo / "requirements.yaml", "value(isp_vac)", "value(isp_sea_level)")
    edit(
        demo / "test_cases.yaml",
        "configuration: [chamber_pressure_nominal]\n    events:\n      ignition",
        "configuration: [chamber_pressure_typo]\n    events:\n      ignition",
    )
    found = errors(demo)
    assert any("parent 'NOPE' does not exist" in e for e in found)
    assert any("'thrust' is a force, threshold 20 bar is a pressure" in e for e in found)
    assert any("value(isp_sea_level)" in e for e in found)
    assert any("unknown design parameter 'chamber_pressure_typo'" in e for e in found)
    assert len(found) >= 4


def test_window_must_exist_in_verifying_test_case(demo):
    edit(demo / "test_cases.yaml", "steady_state: {start", "steady: {start")
    found = errors(demo)
    assert any("evaluated during 'steady_state', which this test case does not define" in e for e in found)


def test_method_condition_consistency(demo):
    edit(
        demo / "requirements.yaml",
        "method: analysis\n    condition: value(thrust_vac)",
        "method: test\n    condition: value(thrust_vac)",
    )
    assert any("test requirements must measure a channel" in e for e in errors(demo))


def test_method_without_condition_and_bad_method(demo):
    edit(
        demo / "requirements.yaml",
        "    method: inspection\n    condition: value(dry_mass) <= 95 kg\n",
        "    method: inspection\n",
    )
    edit(
        demo / "requirements.yaml",
        "method: inspection\n    condition: value(engine_length)",
        "method: guess\n    condition: value(engine_length)",
    )
    found = errors(demo)
    assert any("no 'condition' to check" in e for e in found)
    assert any("method must be one of" in e for e in found)


def test_parent_cycle_detected(demo):
    edit(demo / "requirements.yaml", "  - id: SYS-001\n", "  - id: SYS-001\n    parent: PRP-001\n")
    assert any("cycle" in e for e in errors(demo))


def test_duplicate_ids_and_unknown_keys(demo):
    text = (demo / "requirements.yaml").read_text(encoding="utf-8")
    (demo / "requirements.yaml").write_text(
        text + "\n  - id: STR-012\n    title: dup\n    owner: me\n", encoding="utf-8"
    )
    found = errors(demo)
    assert any("duplicate requirement id" in e for e in found)


def test_bad_yaml_reports_line(demo):
    (demo / "design.yaml").write_text("parameters:\n  a: [1, 2\n", encoding="utf-8")
    assert any("invalid YAML (line" in e for e in errors(demo))


def test_missing_file(demo):
    (demo / "channels.yaml").unlink()
    assert any("channels.yaml: file not found" in e for e in errors(demo))


def test_run_with_missing_data_file(demo):
    (demo / "runs" / "RUN-0002.csv").unlink()
    assert any("data file not found" in e for e in errors(demo))


def test_run_configuration_dimension_checked(demo):
    edit(demo / "runs" / "RUN-0002.yaml", "chamber_pressure_nominal: 120 bar", "chamber_pressure_nominal: 120 kg")
    assert any("is a mass, design parameter is a pressure" in e for e in errors(demo))


def test_run_unknown_test_case(demo):
    edit(demo / "runs" / "RUN-0002.yaml", "test_case: TC-HF-002", "test_case: TC-NOPE")
    assert any("unknown test case 'TC-NOPE'" in e for e in errors(demo))


def test_model_inputs_required(demo):
    edit(demo / "design.yaml", "  gamma: 1.22\n", "")
    assert any("model 'liquid_engine' needs parameter 'gamma'" in e for e in errors(demo))


def test_unknown_model(demo):
    edit(demo / "project.yaml", "model: liquid_engine", "model: solid_motor")
    assert any("unknown model 'solid_motor'" in e for e in errors(demo))


def test_event_channel_and_window_reference_checked(demo):
    edit(
        demo / "test_cases.yaml",
        "ignition: {channel: chamber_pressure, rises_above: 10 bar}\n      shutdown",
        "ignition: {channel: nozzle_pressure, rises_above: 10 bar}\n      shutdown",
    )
    edit(demo / "test_cases.yaml", "startup: {start: ignition,", "startup: {start: lightoff,")
    found = errors(demo)
    assert any("uses unknown channel 'nozzle_pressure'" in e for e in found)
    assert any("refers to unknown event 'lightoff'" in e for e in found)


def test_unverified_test_requirement_warns(demo):
    edit(demo / "test_cases.yaml", "verifies: [PRP-060]", "verifies: []")
    project = Project.load(demo)
    assert any("no test case verifies" in w.message for w in project.warnings)


def test_run_missing_configuration_warns(demo):
    edit(demo / "runs" / "RUN-0002.yaml", "  chamber_pressure_nominal: 120 bar\n", "")
    project = Project.load(demo)
    assert any("does not record chamber_pressure_nominal" in w.message for w in project.warnings)


def test_overrides(demo):
    project = Project.load(demo).with_overrides({"chamber_pressure_nominal": parse_quantity("100 bar")})
    assert project.design["chamber_pressure_nominal"].quantity.si == 100e5
    with pytest.raises(ProjectError, match="unknown design parameter"):
        project.with_overrides({"nope": parse_quantity("1 bar")})
    with pytest.raises(ProjectError, match="is a pressure"):
        project.with_overrides({"chamber_pressure_nominal": parse_quantity("1 kg")})
