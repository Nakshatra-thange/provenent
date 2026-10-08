import math

import pytest

from provenant.physics import LIQUID_ENGINE, ModelError, exit_mach

BASE = {
    "chamber_pressure_nominal": 120e5,
    "chamber_temperature": 3500.0,
    "gamma": 1.22,
    "molar_mass": 22.0,
    "expansion_ratio": 30.0,
    "throat_diameter": 0.036,
    "cstar_efficiency": 0.98,
    "cf_efficiency": 0.98,
}


def area_ratio(mach, gamma):
    return ((2 / (gamma + 1)) * (1 + (gamma - 1) / 2 * mach**2)) ** ((gamma + 1) / (2 * (gamma - 1))) / mach


@pytest.mark.parametrize("eps", [1.5, 4, 10, 30, 100, 300])
@pytest.mark.parametrize("gamma", [1.15, 1.22, 1.4])
def test_exit_mach_inverts_area_ratio(eps, gamma):
    mach = exit_mach(eps, gamma)
    assert mach > 1
    assert area_ratio(mach, gamma) == pytest.approx(eps, rel=1e-9)


def test_exit_mach_reference_value():
    # Air (gamma 1.4) at A/A* = 4: M = 2.940 (standard isentropic tables).
    assert exit_mach(4.0, 1.4) == pytest.approx(2.9402, abs=1e-3)
    assert exit_mach(1.0, 1.4) == 1.0
    with pytest.raises(ModelError):
        exit_mach(0.9, 1.4)


def test_ideal_vacuum_thrust_coefficient_reference():
    # Ideal vacuum Cf for gamma 1.2, eps 10: about 1.76 (Sutton & Biblarz, fig. 3-7).
    out = LIQUID_ENGINE.compute(
        {**BASE, "gamma": 1.2, "expansion_ratio": 10.0, "cstar_efficiency": 1.0, "cf_efficiency": 1.0}
    )
    assert out["cf_vac"] == pytest.approx(1.76, abs=0.02)


def test_outputs_are_consistent():
    out = LIQUID_ENGINE.compute(BASE)
    throat_area = math.pi * 0.036**2 / 4
    assert out["thrust_vac"] == pytest.approx(out["cf_vac"] * BASE["chamber_pressure_nominal"] * throat_area)
    assert out["isp_vac"] == pytest.approx(out["thrust_vac"] / (out["mass_flow"] * 9.80665))
    assert out["exit_diameter"] == pytest.approx(0.036 * math.sqrt(30))
    assert 300 < out["isp_vac"] < 340
    assert 21_000 < out["thrust_vac"] < 23_000


def test_scaling_laws():
    base = LIQUID_ENGINE.compute(BASE)
    doubled = LIQUID_ENGINE.compute({**BASE, "chamber_pressure_nominal": 240e5})
    assert doubled["thrust_vac"] == pytest.approx(2 * base["thrust_vac"])
    assert doubled["isp_vac"] == pytest.approx(base["isp_vac"])  # ideal Isp is pressure-independent
    bigger_nozzle = LIQUID_ENGINE.compute({**BASE, "expansion_ratio": 60.0})
    assert bigger_nozzle["isp_vac"] > base["isp_vac"]


@pytest.mark.parametrize(
    ("key", "value"),
    [("gamma", 1.0), ("expansion_ratio", 0.5), ("throat_diameter", 0.0), ("cstar_efficiency", 1.2)],
)
def test_rejects_unphysical_inputs(key, value):
    with pytest.raises(ModelError):
        LIQUID_ENGINE.compute({**BASE, key: value})
