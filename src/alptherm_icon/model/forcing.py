"""Surface-flux forcing for the model kernel (Komp. C v0.2).

ICON-D2 surface flux fields (ASOB_S, ATHB_S, ASHFL_S, ALHFL_S) are published as
*averages since model initialisation* (W/m², GRIB ``stepType=avg``) — **not**
instantaneous values and **not** time-accumulated J/m². The kernel needs the
instantaneous mean flux over each forecast interval, recovered by de-averaging:

    F(t_{i-1} → t_i) = (mean_i · t_i − mean_{i-1} · t_{i-1}) / (t_i − t_{i-1})

where ``t`` is the lead time in seconds since init. ``mean_i · t_i`` is the
integral of the flux from init to lead ``t_i``; differencing two such integrals
and dividing by the interval gives the interval-mean flux. The v0.1 proxy fed
the running mean directly, which lags solar noon and understates the midday
flux (empirically ~570 W/m² peak net SW de-averaged vs. ~250 W/m² running mean).

This is the official DWD convention: ASOB_S/ATHB_S/ASHFL_S/ALHFL_S are listed as
"time averages over the respective forecast time … from forecast start (t₀=0 s)"
and the de-averaging identity above is given verbatim in the DWD Database
Reference for the Global and Regional ICON and ICON-EPS Forecasting System
(Reinert, Prill, Frank et al.), §7.1.1 "Time-averaged fields" — encoded in GRIB2
as productDefinitionTemplateNumber=8, typeOfStatisticalProcessing=0 (Average),
forecastTime=0. See https://www.dwd.de/DWD/forschung/nwv/fepub/icon_database_main.pdf

Sign convention: DWD surface turbulent fluxes are downward-positive. An upward
flux that heats the CBL is therefore *negative* ASHFL_S/ALHFL_S, so the model's
positive-upward fluxes are ``P_sens = −ASHFL_S`` and ``P_lat = −ALHFL_S``. The
radiation balance ``ASOB_S + ATHB_S`` is already a net downward flux and needs
no sign flip.
"""

from __future__ import annotations

import numpy as np


def deaverage_since_init(
    mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
) -> np.ndarray:
    """De-average a "mean since init" field to per-interval instantaneous flux.

    Args:
        mean_since_init: running mean since init at each lead [W/m²]. Index 0 is
            the init time (lead 0), where the mean is the undefined ``∫/0`` — its
            value is ignored (the integral at lead 0 is 0 regardless).
        lead_seconds: lead time in seconds since init, strictly ascending, same
            shape. ``lead_seconds[0]`` must be 0 (the init time).

    Returns:
        Array of the same shape. Element ``i`` is the mean flux over the interval
        ``(lead[i-1], lead[i]]``; element 0 is 0 (no interval precedes init). A
        NaN in ``mean_since_init[i]`` propagates to intervals ``i`` and ``i+1``.
    """
    mean = np.asarray(mean_since_init, dtype=np.float64)
    t = np.asarray(lead_seconds, dtype=np.float64)
    if mean.shape != t.shape:
        raise ValueError("mean_since_init and lead_seconds must have the same shape")
    if mean.ndim != 1 or mean.size < 2:
        raise ValueError("need a 1-D array of at least two lead times")
    if t[0] != 0:
        raise ValueError("lead_seconds[0] must be 0 (the init time)")
    if np.any(np.diff(t) <= 0):
        raise ValueError("lead_seconds must be strictly ascending")

    cum = mean * t  # ∫₀ᵗ F dt' [W/m²·s]
    cum[0] = 0.0  # integral at lead 0 is 0 even if mean[0] is NaN/undefined
    flux = np.empty_like(mean)
    flux[0] = 0.0
    flux[1:] = np.diff(cum) / np.diff(t)
    return flux


def sensible_flux_proxy(
    asob_s_mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
    fraction: float,
) -> np.ndarray:
    """v0.1 proxy: a fixed fraction of de-averaged net surface shortwave.

    ``ASOB_S`` is a net *downward* flux, so no sign flip is needed.
    """
    return deaverage_since_init(asob_s_mean_since_init, lead_seconds) * fraction


def turbulent_flux_from_icon(
    flux_mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
) -> np.ndarray:
    """Model-positive (upward, CBL-heating) turbulent flux from a DWD field.

    Applies de-averaging and the downward-positive→upward-positive sign flip, so
    ``ASHFL_S``/``ALHFL_S`` become ``P_sens``/``P_lat`` as the kernel expects.
    """
    return -deaverage_since_init(flux_mean_since_init, lead_seconds)


# --------------------------------------------------------------------------- #
# Liechti (1994) reference forcing — validation harness only (spec eqs 1–10)
# --------------------------------------------------------------------------- #
# These re-implement the paper's empirical radiation→flux chain so the parcel
# kernel can reproduce Table 2 / Figure 4 from the paper's own inputs. They are
# NOT used operationally — ICON ASOB_S/ASHFL_S/… (above) supersede all of this.

from dataclasses import dataclass  # noqa: E402

from alptherm_icon.model.thermo import vapor_pressure_from_dewpoint  # noqa: E402

_S0 = 1200.0  # solar constant proxy [W/m²] (eq 3)
_GAMMA_MAX = 0.323  # = |ln 0.74| (eq 2)
_Z_GAMMA = 2333.0  # transmission scale height [m] (eq 2)
_ALBEDO = 0.15  # A (eq 4)
_SIGMA = 5.67e-8  # Stefan–Boltzmann [W/m²K⁴] (eq 5)
_DELTA = 0.005  # soil–air ΔT coefficient [K·m²/W] (eq 8)
_EVAP = 0.60  # evaporation fraction (eq 9)
_G_GROUND = 0.15  # ground-heat fraction (eq 9/10)


@dataclass
class ReferenceForcing:
    """Per-timestep Liechti surface energy balance."""

    p_net_w_m2: float  # P = Q_k − Q_f (eq 7)
    p_sens_w_m2: float  # eq 10
    p_lat_w_m2: float  # eq 9
    t_skin_K: float  # T_S (eq 8)


def solar_declination_deg(day_of_year: int) -> float:
    """Solar declination [deg] (Cooper). day_of_year ∈ 1…365."""
    return 23.44 * np.sin(np.deg2rad(360.0 * (284 + day_of_year) / 365.0))


def sin_solar_elevation(lat_deg: float, declination_deg: float, hour_angle_deg: float) -> float:
    """sin ε for a flat surface: sinφ sinδ + cosφ cosδ cos(H) (eq 1, flat case)."""
    phi = np.deg2rad(lat_deg)
    dec = np.deg2rad(declination_deg)
    ha = np.deg2rad(hour_angle_deg)
    return float(np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.cos(ha))


def sin_elevation_series(
    lat_deg: float, day_of_year: int, solar_hours: np.ndarray
) -> tuple[np.ndarray, float]:
    """sin ε at each local solar hour, plus the day's max (at solar noon)."""
    dec = solar_declination_deg(day_of_year)
    ha = 15.0 * (np.asarray(solar_hours, dtype=np.float64) - 12.0)  # 15°/h
    sin_eps = np.array([sin_solar_elevation(lat_deg, dec, h) for h in ha])
    sin_eps_max = sin_solar_elevation(lat_deg, dec, 0.0)
    return np.clip(sin_eps, 0.0, None), float(max(sin_eps_max, 1e-6))


def liechti_surface_flux(
    T_air_K: float,
    Td_air_K: float,
    z_m: float,
    sin_eps: float,
    sin_eps_max: float,
    n_iter: int = 3,
) -> ReferenceForcing:
    """Radiation budget → sensible/latent split (spec eqs 1–10) for one step.

    ``T_air_K`` is the *current* near-surface air temperature (the model state,
    so the budget tracks daytime heating); ``Td_air_K`` the surface dewpoint.
    The skin temperature ``T_S`` (eq 8) is solved by a few fixed-point iterations
    because the outgoing longwave (eq 5) depends on it.
    """
    if sin_eps <= 0.0:
        # Night: no shortwave; net budget is the longwave loss at T_S ≈ T_air.
        q_k = 0.0
    else:
        gamma = _GAMMA_MAX * np.exp(-z_m / _Z_GAMMA)
        transmission = np.exp(-gamma * sin_eps_max / sin_eps)
        s_in = _S0 * sin_eps * transmission
        q_k = s_in * (1.0 - _ALBEDO)

    e_hpa = float(vapor_pressure_from_dewpoint(Td_air_K))
    mu = 0.594 + 0.0416 * np.sqrt(max(e_hpa, 0.0))

    t_skin = T_air_K
    p_net = 0.0
    for _ in range(n_iter):
        q_f = _SIGMA * (t_skin**4 - mu * T_air_K**4)
        p_net = q_k - q_f
        t_skin = T_air_K + _DELTA * p_net

    available = (1.0 - _G_GROUND) * p_net
    p_lat = _EVAP * available
    p_sens = (1.0 - _EVAP) * available
    return ReferenceForcing(
        p_net_w_m2=p_net, p_sens_w_m2=p_sens, p_lat_w_m2=p_lat, t_skin_K=t_skin
    )
