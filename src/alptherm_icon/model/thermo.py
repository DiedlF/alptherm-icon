"""Minimal thermodynamic helpers for Komp. C v0.1.

Dry-air constants only. No moisture handling yet (no LCL, no moist
adiabat, no virtual temperature) — those land in v0.3 together with
bin-wise parcel theory.
"""

from __future__ import annotations

import numpy as np

G = 9.80665
"""Standard gravity [m/s²]."""

R_D = 287.04
"""Specific gas constant for dry air [J/(kg·K)]."""

C_P = 1005.0
"""Specific heat capacity at constant pressure, dry air [J/(kg·K)]."""

P_0 = 100000.0
"""Reference pressure for potential temperature [Pa]."""

KAPPA = R_D / C_P
"""R_d / c_p ≈ 0.286."""

ISA_T0 = 288.15
"""International Standard Atmosphere sea-level temperature [K]."""

ISA_LAPSE = 0.0065
"""ISA tropospheric lapse rate [K/m]."""

ISA_P0 = 101325.0
"""ISA sea-level pressure [Pa]."""


def standard_pressure(z_m: np.ndarray | float) -> np.ndarray | float:
    """ISA pressure at geometric height z [m] above sea level. Valid 0..11 km."""
    return ISA_P0 * (1.0 - ISA_LAPSE * np.asarray(z_m) / ISA_T0) ** (G / (R_D * ISA_LAPSE))


def potential_temperature(
    T_K: np.ndarray | float,
    p_Pa: np.ndarray | float,
) -> np.ndarray | float:
    """θ = T · (p_0/p)^(R_d/c_p). T in K, p in Pa."""
    return np.asarray(T_K) * (P_0 / np.asarray(p_Pa)) ** KAPPA


def temperature_from_theta(
    theta_K: np.ndarray | float,
    p_Pa: np.ndarray | float,
) -> np.ndarray | float:
    """Inverse of potential_temperature: T = θ · (p/p_0)^κ."""
    return np.asarray(theta_K) * (np.asarray(p_Pa) / P_0) ** KAPPA


def w_star(
    sensible_heat_flux_w_m2: float,
    z_i_m: float,
    theta_surface_K: float,
    rho_kg_m3: float = 1.0,
) -> float:
    """Deardorff convective velocity scale.

    w* = (g · w'θ'_surface · z_i / θ_v)^(1/3)

    where w'θ'_surface = H / (ρ · c_p) is the kinematic surface heat flux.
    Returns 0 when surface flux is non-positive or z_i is non-positive
    (nighttime / pre-sunrise / decaying CBL).
    """
    if sensible_heat_flux_w_m2 <= 0 or z_i_m <= 0:
        return 0.0
    h_kinematic = sensible_heat_flux_w_m2 / (rho_kg_m3 * C_P)
    return float((G / theta_surface_K * h_kinematic * z_i_m) ** (1.0 / 3.0))
