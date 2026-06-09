"""Thermodynamic helpers for Komp. C.

Dry-air building blocks (potential temperature, ISA pressure, w*) plus the
moist helpers the v0.3 parcel theory needs: saturation vapour pressure, LCL,
virtual/density temperature (with condensate loading) and the moist-adiabatic
lapse rate. All functions are numpy-friendly (scalar or array in, same out).
"""

from __future__ import annotations

import numpy as np

G = 9.80665
"""Standard gravity [m/s²]."""

R_D = 287.04
"""Specific gas constant for dry air [J/(kg·K)]."""

R_V = 461.5
"""Specific gas constant for water vapour [J/(kg·K)]."""

C_P = 1005.0
"""Specific heat capacity at constant pressure, dry air [J/(kg·K)]."""

L_V = 2.5e6
"""Latent heat of vaporisation [J/kg] (spec eq 14)."""

EPSILON = R_D / R_V
"""R_d / R_v ≈ 0.622 (molar mass ratio water/dry air)."""

P_0 = 100000.0
"""Reference pressure for potential temperature [Pa]."""

KAPPA = R_D / C_P
"""R_d / c_p ≈ 0.286."""

T_ZERO_C = 273.15
"""0 °C in kelvin."""

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


# --------------------------------------------------------------------------- #
# Moisture (v0.3 parcel theory)
# --------------------------------------------------------------------------- #


def saturation_vapor_pressure(T_K: np.ndarray | float) -> np.ndarray | float:
    """Saturation vapour pressure over water [hPa], Magnus/WMO form.

    e_s = 6.112 · exp(17.62·t / (243.12 + t)), t = T − 273.15 [°C].
    Valid roughly −45…+60 °C.
    """
    t_c = np.asarray(T_K, dtype=np.float64) - T_ZERO_C
    return 6.112 * np.exp(17.62 * t_c / (243.12 + t_c))


def vapor_pressure_from_dewpoint(Td_K: np.ndarray | float) -> np.ndarray | float:
    """Actual vapour pressure [hPa] from dewpoint — Magnus evaluated at T_d."""
    return saturation_vapor_pressure(Td_K)


def dewpoint_from_vapor_pressure(e_hPa: np.ndarray | float) -> np.ndarray | float:
    """Inverse Magnus: dewpoint [K] from vapour pressure [hPa]."""
    e = np.maximum(np.asarray(e_hPa, dtype=np.float64), 1e-6)
    ln = np.log(e / 6.112)
    t_c = 243.12 * ln / (17.62 - ln)
    return t_c + T_ZERO_C


def mixing_ratio(e_hPa: np.ndarray | float, p_Pa: np.ndarray | float) -> np.ndarray | float:
    """Water-vapour mixing ratio [kg/kg] from vapour pressure and air pressure."""
    e_Pa = np.asarray(e_hPa, dtype=np.float64) * 100.0
    p = np.asarray(p_Pa, dtype=np.float64)
    return EPSILON * e_Pa / (p - e_Pa)


def saturation_mixing_ratio(
    T_K: np.ndarray | float, p_Pa: np.ndarray | float
) -> np.ndarray | float:
    """Saturation mixing ratio r_s(T, p) [kg/kg]."""
    return mixing_ratio(saturation_vapor_pressure(T_K), p_Pa)


def relative_humidity(
    T_K: np.ndarray | float, Td_K: np.ndarray | float
) -> np.ndarray | float:
    """RH as a fraction (0…1) from temperature and dewpoint."""
    return saturation_vapor_pressure(Td_K) / saturation_vapor_pressure(T_K)


def lcl_height(
    T_K: np.ndarray | float, Td_K: np.ndarray | float, z_m: np.ndarray | float
) -> np.ndarray | float:
    """Lifting condensation level [m ASL] — Espy dewpoint-depression rule.

    z_lcl = z + 125·(T − T_d), with (T − T_d) the surface dewpoint depression
    in K (= °C). The 125 m/°C constant is the standard Espy approximation; it
    is what sets the parcel's cloud base in the bin model.
    """
    depression = np.asarray(T_K, dtype=np.float64) - np.asarray(Td_K, dtype=np.float64)
    return np.asarray(z_m, dtype=np.float64) + 125.0 * np.maximum(depression, 0.0)


def virtual_temperature(
    T_K: np.ndarray | float, r_vapor: np.ndarray | float = 0.0
) -> np.ndarray | float:
    """Virtual temperature T_v = T·(1 + r/ε)/(1 + r). r = vapour mixing ratio."""
    T = np.asarray(T_K, dtype=np.float64)
    r = np.asarray(r_vapor, dtype=np.float64)
    return T * (1.0 + r / EPSILON) / (1.0 + r)


def density_temperature(
    T_K: np.ndarray | float,
    r_vapor: np.ndarray | float = 0.0,
    r_liquid: np.ndarray | float = 0.0,
) -> np.ndarray | float:
    """Density temperature T_ρ = T·(1 + r_v/ε)/(1 + r_v + r_l).

    Includes condensate (liquid water) loading, so a cloudy parcel is denser
    than its virtual temperature alone implies — the negative-buoyancy term in
    eq (15). ρ = p / (R_d · T_ρ).
    """
    T = np.asarray(T_K, dtype=np.float64)
    r_v = np.asarray(r_vapor, dtype=np.float64)
    r_l = np.asarray(r_liquid, dtype=np.float64)
    return T * (1.0 + r_v / EPSILON) / (1.0 + r_v + r_l)


def air_density(
    T_K: np.ndarray | float,
    p_Pa: np.ndarray | float,
    r_vapor: np.ndarray | float = 0.0,
    r_liquid: np.ndarray | float = 0.0,
) -> np.ndarray | float:
    """Moist air density [kg/m³] via the density temperature (condensate-loaded)."""
    t_rho = density_temperature(T_K, r_vapor, r_liquid)
    return np.asarray(p_Pa, dtype=np.float64) / (R_D * t_rho)


def moist_adiabatic_lapse_rate(
    T_K: np.ndarray | float, p_Pa: np.ndarray | float
) -> np.ndarray | float:
    """Saturated (moist) adiabatic lapse rate Γ_m [K/m], positive downward.

    Γ_m = g·(1 + L·r_s/(R_d·T)) / (c_p + L²·r_s·ε/(R_d·T²)). Approaches the dry
    rate g/c_p when r_s → 0 and is smaller (latent release) when moist.
    """
    T = np.asarray(T_K, dtype=np.float64)
    r_s = saturation_mixing_ratio(T, p_Pa)
    num = 1.0 + L_V * r_s / (R_D * T)
    den = C_P + L_V**2 * r_s * EPSILON / (R_D * T**2)
    return G * num / den
