"""Writes the TE-2 upper-stage engine demo project.

The demo is designed to exercise every behaviour worth showing: analysis,
inspection and test verification; a superseded run; a voided run; a run whose
pressure column was renamed by a DAQ reconfiguration (resolved via an alias);
a sensor dropout; a planned test case that has never been run; and margins
thin enough that realistic changes flip verdicts.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from .simulate import FiringProfile, generate_hot_fire

PROJECT_YAML = """\
name: TE-2 upper-stage engine
program: Tern
model: liquid_engine
"""

REQUIREMENTS_YAML = """\
requirements:
  - id: SYS-001
    title: TE-2 meets Tern upper-stage needs
    text: The TE-2 engine shall meet the propulsion needs of the Tern upper stage.

  - id: PRP-001
    parent: SYS-001
    title: Vacuum performance
    text: The engine shall deliver the vacuum performance assumed in the Tern mission design.

  - id: PRP-010
    parent: PRP-001
    title: Measured vacuum thrust
    text: Steady-state vacuum thrust measured in the altitude cell shall be at least 20 kN.
    method: test
    condition: mean(thrust) >= 20 kN during steady_state

  - id: PRP-011
    parent: PRP-001
    title: Predicted vacuum thrust
    text: Predicted vacuum thrust at the nominal operating point shall be at least 20 kN.
    method: analysis
    condition: value(thrust_vac) >= 20 kN

  - id: PRP-044
    parent: PRP-001
    title: Vacuum specific impulse
    text: Predicted vacuum specific impulse shall be at least 315 s.
    method: analysis
    condition: value(isp_vac) >= 315 s

  - id: PRP-050
    parent: PRP-001
    title: Burn duration
    text: The engine shall sustain at least 90 % of nominal chamber pressure for 25 s or more.
    method: test
    condition: duration(chamber_pressure >= 108 bar) >= 25 s

  - id: PRP-030
    parent: SYS-001
    title: Chamber pressure limits
    text: Chamber pressure shall stay within the structural and start-up limits of the thrust chamber.

  - id: PRP-031
    parent: PRP-030
    title: Maximum chamber pressure
    text: Chamber pressure shall never exceed 150 bar.
    method: test
    condition: max(chamber_pressure) <= 150 bar

  - id: PRP-033
    parent: PRP-030
    title: Start-up overshoot
    text: Chamber pressure overshoot during start-up shall not exceed 138 bar.
    method: test
    condition: max(chamber_pressure) <= 138 bar during startup

  - id: TCS-020
    parent: SYS-001
    title: Throat wall temperature
    text: Throat wall temperature shall not exceed 900 K during steady-state operation.
    method: test
    condition: max(throat_wall_temp) <= 900 K during steady_state

  - id: STR-012
    parent: SYS-001
    title: Engine dry mass
    text: Engine dry mass shall not exceed 95 kg.
    method: inspection
    condition: value(dry_mass) <= 95 kg

  - id: SYS-030
    parent: SYS-001
    title: Engine length in the interstage
    text: Overall engine length shall not exceed 1.4 m.
    method: inspection
    condition: value(engine_length) <= 1.4 m

  - id: PRP-060
    parent: SYS-001
    title: Restart capability
    text: The engine shall reach at least 90 % of nominal chamber pressure on a second ignition.
    method: test
    condition: max(chamber_pressure) >= 108 bar during second_burn
"""

TEST_CASES_YAML = """\
test_cases:
  - id: TC-HF-001
    title: Full-duration hot fire
    description: 30 s firing at the nominal operating point in the altitude cell.
    verifies: [PRP-010, PRP-031, PRP-050, TCS-020]
    configuration: [chamber_pressure_nominal, expansion_ratio, throat_diameter]
    events:
      ignition: {channel: chamber_pressure, rises_above: 10 bar}
      shutdown: {channel: chamber_pressure, falls_below: 10 bar, after: ignition}
    windows:
      steady_state: {start: ignition + 2 s, end: shutdown - 1 s}

  - id: TC-HF-002
    title: Start-up transient
    description: Short firing instrumented for the start-up transient.
    verifies: [PRP-031, PRP-033]
    configuration: [chamber_pressure_nominal]
    events:
      ignition: {channel: chamber_pressure, rises_above: 10 bar}
    windows:
      startup: {start: ignition, end: ignition + 1.5 s}

  - id: TC-RS-001
    title: Restart
    description: Two firings separated by a 60 s coast.
    verifies: [PRP-060]
    configuration: [chamber_pressure_nominal]
    events:
      first_shutdown: {channel: chamber_pressure, falls_below: 10 bar}
      reignition: {channel: chamber_pressure, rises_above: 10 bar, after: first_shutdown}
    windows:
      second_burn: {start: reignition + 1 s, end: end}
"""

CHANNELS_YAML = """\
time:
  column: time_s
  unit: s

channels:
  chamber_pressure:
    column: PT-101
    aliases: [PT101]   # renamed in the August DAQ reconfiguration
    unit: psi
    description: Chamber pressure transducer, injector face
  thrust:
    column: LC-01
    unit: lbf
    description: Thrust stand load cell, vacuum-equivalent
  throat_wall_temp:
    column: TC-205
    unit: degC
    description: Thermocouple embedded 1 mm below the throat hot-gas wall
  mass_flow:
    column: FM-01
    unit: kg/s
    description: Total propellant mass flow
"""

DESIGN_YAML = """\
parameters:
  chamber_pressure_nominal: {value: 120 bar, tolerance: 2%}
  chamber_temperature: 3500 K
  gamma: 1.22
  molar_mass: 22 kg/kmol
  expansion_ratio: {value: 30, tolerance: 0.5%}
  throat_diameter: {value: 36 mm, tolerance: 0.05 mm}
  cstar_efficiency: 0.98
  cf_efficiency: 0.98
  dry_mass: {value: 76.5 kg, source: "Weigh-in, SN03, 2026-07-30"}
  engine_length: {value: 1.26 m, source: "CMM inspection report IR-0412"}
"""

_DESIGN_SI = {
    "chamber_pressure_nominal": 120e5,
    "chamber_temperature": 3500.0,
    "gamma": 1.22,
    "molar_mass": 22.0,
    "expansion_ratio": 30.0,
    "throat_diameter": 0.036,
    "cstar_efficiency": 0.98,
    "cf_efficiency": 0.98,
}

_RUNS: list[dict[str, Any]] = [
    {
        "id": "RUN-0001",
        "test_case": "TC-HF-001",
        "date": "2026-07-18",
        "article": "TE2-SN02",
        "pc": 118.0,
        "notes": "First full-duration firing on SN02.",
        "profile": {"burn_s": 30.0, "wall_temp_steady_k": 831.0, "seed": 11},
    },
    {
        "id": "RUN-0002",
        "test_case": "TC-HF-002",
        "date": "2026-07-25",
        "article": "TE2-SN03",
        "pc": 120.0,
        "notes": "Start-up transient, high-rate pressure acquisition.",
        "profile": {"burn_s": 5.0, "wall_temp_steady_k": 700.0, "seed": 22, "sample_rate_hz": 500.0},
    },
    {
        "id": "RUN-0003",
        "test_case": "TC-HF-001",
        "date": "2026-08-21",
        "article": "TE2-SN03",
        "pc": 120.0,
        "notes": "Repeat on SN03 after injector rework. DAQ reconfigured: PT-101 now logged as PT101.\n"
        "TC-205 dropout at T+14 s (connector), recovered.",
        "profile": {
            "burn_s": 30.0,
            "wall_temp_steady_k": 858.0,
            "seed": 33,
            "pressure_column": "PT101",
            "dropouts": [("TC-205", 15.0, 15.4)],
        },
    },
    {
        "id": "RUN-0004",
        "test_case": "TC-HF-001",
        "date": "2026-08-28",
        "article": "TE2-SN03",
        "pc": 120.0,
        "void": "Facility abort at T+4 s: LOX main valve position fault (facility, not engine).",
        "notes": "Aborted by facility redline.",
        "profile": {
            "burn_s": 30.0,
            "wall_temp_steady_k": 858.0,
            "seed": 44,
            "pressure_column": "PT101",
            "abort_at_s": 4.0,
        },
    },
]


def _run_yaml(run: dict[str, Any]) -> str:
    notes = textwrap.indent(str(run["notes"]), "  ")
    lines = [
        f"id: {run['id']}",
        f"test_case: {run['test_case']}",
        f"date: {run['date']}",
        f"article: {run['article']}",
        f"data: {run['id']}.csv",
        "configuration:",
        f"  chamber_pressure_nominal: {run['pc']:g} bar",
        "  expansion_ratio: 30",
        "  throat_diameter: 36 mm",
    ]
    if run.get("void"):
        lines.append(f'void: "{run["void"]}"')
    lines.append(f"notes: |\n{notes}")
    return "\n".join(lines) + "\n"


def write_demo(target: Path) -> Path:
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} already exists and is not empty")
    (target / "runs").mkdir(parents=True, exist_ok=True)
    files = {
        "project.yaml": PROJECT_YAML,
        "requirements.yaml": REQUIREMENTS_YAML,
        "test_cases.yaml": TEST_CASES_YAML,
        "channels.yaml": CHANNELS_YAML,
        "design.yaml": DESIGN_YAML,
    }
    for name, content in files.items():
        (target / name).write_text(content, encoding="utf-8")
    for run in _RUNS:
        profile = FiringProfile(chamber_pressure_bar=float(run["pc"]), design=_DESIGN_SI, **run["profile"])
        (target / "runs" / f"{run['id']}.csv").write_text(generate_hot_fire(profile), encoding="utf-8")
        (target / "runs" / f"{run['id']}.yaml").write_text(_run_yaml(run), encoding="utf-8")
    return target
