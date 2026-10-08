"""Analysis models that turn design parameters into predicted performance.

Requirements verified by *analysis* use ``value(<output>)`` and are re-evaluated
automatically whenever a design parameter the model reads changes.

The built-in ``liquid_engine`` model is an ideal-rocket (isentropic, frozen
composition) nozzle model with empirical c* and Cf efficiencies — the standard
first-order sizing model from Sutton & Biblarz, *Rocket Propulsion Elements*,
ch. 3. It is deliberately simple and fully deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

G0 = 9.80665  # m/s^2, standard gravity
R_UNIVERSAL = 8314.462618  # J/(kmol K)


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class Model:
    name: str
    version: str
    inputs: dict[str, str]  # parameter -> dimension
    outputs: dict[str, tuple[str, str]]  # output -> (dimension, display unit)
    compute: Callable[[dict[str, float]], dict[str, float]]
    description: str = ""


def exit_mach(area_ratio: float, gamma: float) -> float:
    """Supersonic Mach number for a given nozzle area ratio A/A*."""
    if area_ratio < 1.0:
        raise ModelError(f"expansion ratio must be >= 1, got {area_ratio}")
    if area_ratio == 1.0:
        return 1.0
    exponent = (gamma + 1.0) / (2.0 * (gamma - 1.0))

    def residual(mach: float) -> float:
        ratio = (2.0 / (gamma + 1.0)) * (1.0 + 0.5 * (gamma - 1.0) * mach * mach)
        return float(ratio**exponent / mach - area_ratio)

    lo, hi = 1.0, 2.0
    while residual(hi) < 0.0:
        hi *= 2.0
        if hi > 1e4:  # pragma: no cover - unreachable for physical inputs
            raise ModelError("exit Mach number did not converge")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if residual(mid) < 0.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-13:
            break
    return 0.5 * (lo + hi)


def _liquid_engine(p: dict[str, float]) -> dict[str, float]:
    pc = p["chamber_pressure_nominal"]
    tc = p["chamber_temperature"]
    gamma = p["gamma"]
    molar_mass = p["molar_mass"]
    eps = p["expansion_ratio"]
    dt = p["throat_diameter"]
    eta_c = p["cstar_efficiency"]
    eta_f = p["cf_efficiency"]

    checks = [
        (pc > 0, "chamber_pressure_nominal must be positive"),
        (tc > 0, "chamber_temperature must be positive"),
        (1.0 < gamma < 2.0, "gamma must be between 1 and 2"),
        (molar_mass > 0, "molar_mass must be positive"),
        (eps >= 1.0, "expansion_ratio must be at least 1"),
        (dt > 0, "throat_diameter must be positive"),
        (0.5 < eta_c <= 1.0, "cstar_efficiency must be in (0.5, 1]"),
        (0.5 < eta_f <= 1.0, "cf_efficiency must be in (0.5, 1]"),
    ]
    for ok, message in checks:
        if not ok:
            raise ModelError(message)

    gas_constant = R_UNIVERSAL / molar_mass
    choke = (2.0 / (gamma + 1.0)) ** ((gamma + 1.0) / (gamma - 1.0))
    cstar_ideal = math.sqrt(gamma * gas_constant * tc) / (gamma * math.sqrt(choke))

    mach_e = exit_mach(eps, gamma)
    pe_over_pc = (1.0 + 0.5 * (gamma - 1.0) * mach_e**2) ** (-gamma / (gamma - 1.0))
    momentum = math.sqrt(2.0 * gamma**2 / (gamma - 1.0) * choke * (1.0 - pe_over_pc ** ((gamma - 1.0) / gamma)))
    cf_ideal = momentum + eps * pe_over_pc  # vacuum: ambient pressure is zero

    throat_area = math.pi * dt * dt / 4.0
    cstar = eta_c * cstar_ideal
    cf = eta_f * cf_ideal
    thrust = cf * pc * throat_area
    mass_flow = pc * throat_area / cstar
    return {
        "thrust_vac": thrust,
        "isp_vac": thrust / (mass_flow * G0),
        "mass_flow": mass_flow,
        "cstar": cstar,
        "cf_vac": cf,
        "exit_mach": mach_e,
        "exit_pressure": pe_over_pc * pc,
        "exit_diameter": dt * math.sqrt(eps),
    }


LIQUID_ENGINE = Model(
    name="liquid_engine",
    version="1",
    inputs={
        "chamber_pressure_nominal": "pressure",
        "chamber_temperature": "temperature",
        "gamma": "dimensionless",
        "molar_mass": "molar_mass",
        "expansion_ratio": "dimensionless",
        "throat_diameter": "length",
        "cstar_efficiency": "dimensionless",
        "cf_efficiency": "dimensionless",
    },
    outputs={
        "thrust_vac": ("force", "kN"),
        "isp_vac": ("time", "s"),
        "mass_flow": ("mass_flow", "kg/s"),
        "cstar": ("velocity", "m/s"),
        "cf_vac": ("dimensionless", "1"),
        "exit_mach": ("dimensionless", "1"),
        "exit_pressure": ("pressure", "kPa"),
        "exit_diameter": ("length", "mm"),
    },
    compute=_liquid_engine,
    description="Ideal-rocket vacuum performance with c* and Cf efficiencies",
)

MODELS: dict[str, Model] = {LIQUID_ENGINE.name: LIQUID_ENGINE}
