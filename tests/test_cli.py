import json
import subprocess
import sys

import pytest

from conftest import edit
from provenant.cli import EXIT_FAILED, EXIT_OK, EXIT_STALE, EXIT_USAGE, main
from provenant.reqif import ReqRecord, export_reqif


def run(demo, *args):
    return main(["-C", str(demo), "--color", "never", *args])


def test_demo_command(tmp_path, capsys):
    assert main(["demo", str(tmp_path / "x")]) == EXIT_OK
    assert (tmp_path / "x" / "project.yaml").is_file()
    assert main(["demo", str(tmp_path / "x")]) == EXIT_USAGE  # refuses to overwrite


def test_verify_writes_lock_and_reports(demo, capsys):
    assert run(demo, "verify") == EXIT_OK
    out = capsys.readouterr().out
    assert "PRP-044" in out and "+0.9%" in out
    assert "9/10 requirements verified (90%)" in out
    assert "TC-RS-001" in out
    lock = json.loads((demo / "provenant.lock").read_text(encoding="utf-8"))
    assert lock["requirements"]["PRP-010"]["status"] == "verified"
    assert run(demo, "verify") == EXIT_OK
    assert "Unchanged: provenant.lock" in capsys.readouterr().out


def test_verify_color_output(demo, capsys):
    assert main(["-C", str(demo), "--color", "always", "verify", "--no-lock"]) == EXIT_OK
    assert "\033[" in capsys.readouterr().out
    assert not (demo / "provenant.lock").exists()


def test_verify_exit_code_on_failure(demo, capsys):
    edit(demo / "requirements.yaml", "<= 900 K", "<= 850 K")
    assert run(demo, "verify") == EXIT_FAILED


def test_verify_what_if_does_not_lock(demo, capsys):
    assert run(demo, "verify", "--set", "chamber_pressure_nominal=100 bar") == EXIT_FAILED
    assert "what-if" in capsys.readouterr().out
    assert not (demo / "provenant.lock").exists()


def test_status_gate(demo, capsys):
    assert run(demo, "status") == EXIT_STALE  # no lock yet
    run(demo, "verify")
    assert run(demo, "status") == EXIT_OK
    edit(demo / "design.yaml", "value: 120 bar", "value: 100 bar")
    capsys.readouterr()
    assert run(demo, "status") == EXIT_STALE
    out = capsys.readouterr().out
    assert "chamber_pressure_nominal: 120 bar -> 100 bar" in out
    assert "PRP-011" in out and "physical retest needed" in out


def test_impact_what_if(demo, capsys):
    run(demo, "verify")
    capsys.readouterr()
    assert run(demo, "impact", "--set", "chamber_pressure_nominal=100bar") == EXIT_OK
    out = capsys.readouterr().out
    assert "What-if impact" in out
    assert "thrust_vac: 22.03 kN -> 18.36 kN" in out
    assert "TC-HF-001" in out
    assert run(demo, "status") == EXIT_OK  # what-if left the project untouched


def test_impact_without_changes(demo, capsys):
    run(demo, "verify")
    capsys.readouterr()
    assert run(demo, "impact") == EXIT_OK
    assert "No impact" in capsys.readouterr().out


def test_impact_requires_lock(demo, capsys):
    assert run(demo, "impact") == EXIT_USAGE


def test_trace(demo, capsys):
    assert run(demo, "trace", "TCS-020", "PRP-044", "SYS-001", "PRP-060") == EXIT_OK
    out = capsys.readouterr().out
    assert "column TC-205 [degC]" in out
    assert "superseded by RUN-0003" in out
    assert "void: Facility abort" in out
    assert "throat_diameter = 36 mm" in out
    assert "children PRP-001" in out
    assert "not yet run" in out
    assert run(demo, "trace", "NOPE-1") == EXIT_USAGE


def test_check(demo, capsys):
    assert run(demo, "check") == EXIT_OK
    assert "13 requirements" in capsys.readouterr().out


def test_check_reports_every_error(demo, capsys):
    edit(demo / "requirements.yaml", "value(isp_vac)", "value(isp)")
    edit(demo / "channels.yaml", "unit: lbf", "unit: furlongs")
    assert run(demo, "check") == EXIT_USAGE
    err = capsys.readouterr().err
    assert "value(isp)" in err and "unknown unit 'furlongs'" in err


def test_bad_override_is_usage_error(demo, capsys):
    run(demo, "verify")
    assert run(demo, "impact", "--set", "chamber_pressure_nominal") == EXIT_USAGE
    assert run(demo, "impact", "--set", "chamber_pressure_nominal=3 kg") == EXIT_USAGE


def test_report(demo, capsys):
    run(demo, "verify")
    out_file = demo / "r.html"
    assert run(demo, "report", "-o", str(out_file)) == EXIT_OK
    html = out_file.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "TE-2 upper-stage engine" in html
    assert html.count("<svg") >= 8
    assert "Every verdict matches the lock" in html


def test_report_shows_changes_and_what_if(demo, capsys):
    run(demo, "verify")
    edit(demo / "design.yaml", "value: 120 bar", "value: 100 bar")
    out_file = demo / "r.html"
    assert run(demo, "report", "-o", str(out_file)) == EXIT_OK
    html = out_file.read_text(encoding="utf-8")
    assert "Verdicts that changed" in html
    assert "Evidence invalidated by design change" in html
    assert "120 bar → 100 bar" in html
    assert run(demo, "report", "-o", str(out_file), "--set", "throat_diameter=40mm") == EXIT_OK
    assert "What-if evaluation" in out_file.read_text(encoding="utf-8")


def test_report_without_lock(demo, capsys):
    out_file = demo / "r.html"
    assert run(demo, "report", "-o", str(out_file)) == EXIT_OK
    assert "No lock recorded yet" in out_file.read_text(encoding="utf-8")


def test_report_escapes_user_content(demo, capsys):
    edit(demo / "requirements.yaml", "title: Engine dry mass", 'title: "<script>alert(1)</script>"')
    out_file = demo / "r.html"
    run(demo, "report", "-o", str(out_file))
    html = out_file.read_text(encoding="utf-8")
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_reqif_cli_round_trip(demo, tmp_path, capsys):
    target = tmp_path / "spec.reqif"
    assert run(demo, "reqif", "export", "-o", str(target)) == EXIT_OK
    assert run(demo, "reqif", "diff", str(target), str(demo)) == EXIT_OK
    assert "No differences" in capsys.readouterr().out
    edit(demo / "requirements.yaml", "<= 95 kg", "<= 90 kg")
    assert run(demo, "reqif", "diff", str(target), str(demo), "--exit-code") == EXIT_FAILED
    assert run(demo, "reqif", "import", str(target), "--dry-run") == EXIT_OK
    assert "<= 90 kg" in (demo / "requirements.yaml").read_text(encoding="utf-8")  # dry run left it alone
    assert run(demo, "reqif", "import", str(target)) == EXIT_OK
    assert "<= 95 kg" in (demo / "requirements.yaml").read_text(encoding="utf-8")
    capsys.readouterr()
    assert run(demo, "reqif", "import", str(target)) == EXIT_OK
    assert "No changes." in capsys.readouterr().out


def test_reqif_import_rolls_back_invalid_merge(demo, tmp_path, capsys):
    bad = tmp_path / "bad.reqif"
    bad.write_bytes(export_reqif([ReqRecord("X-1", "Bad", method="test", condition="max(nope) <= 3 bar")], "bad"))
    before = (demo / "requirements.yaml").read_text(encoding="utf-8")
    assert run(demo, "reqif", "import", str(bad)) == EXIT_USAGE
    assert (demo / "requirements.yaml").read_text(encoding="utf-8") == before
    assert "rolled back" in capsys.readouterr().err


def test_unreadable_reqif_is_usage_error(demo, tmp_path, capsys):
    bad = tmp_path / "bad.reqif"
    bad.write_text("<nope/>", encoding="utf-8")
    assert run(demo, "reqif", "import", str(bad)) == EXIT_USAGE
    assert "expected <REQ-IF>" in capsys.readouterr().err


def test_global_options_after_subcommand(demo, capsys):
    assert main(["verify", "--no-lock", "-C", str(demo), "--color", "never"]) == EXIT_OK
    assert "\033[" not in capsys.readouterr().out
    assert main(["--color", "always", "-C", str(demo), "check"]) == EXIT_OK
    assert "\033[" in capsys.readouterr().out  # an option before the subcommand is not reset


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0


def test_module_entry_point():
    result = subprocess.run([sys.executable, "-m", "provenant", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.startswith("provenant ")
