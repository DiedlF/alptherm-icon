"""Bin-wise convection kernel — Komp. C v0.3 (spec eqs 11–19).

Liechti's ALPTHERM as a column model on 100 m layers. The region's area-height
distribution (AHD ``S_G(z)`` heated terrain area, ``V_a(z)`` residual air volume,
from Komp. A) drives the *volume effect*: a layer with little air volume but much
heated terrain warms fast — the physical reason valleys trigger earlier/stronger
thermals than the flat foreland.

Per time step Δt:
  1. surface energy balance → P_sens, P_lat (eqs 1–10; ICON or Liechti reference)
  2. AHD-weighted heating of each layer's air volume (eq 11), then convective
     adjustment grows the mixed layer
  3. rising parcels (eqs 12–17) give the updraft speed v(z) and, above their LCL,
     condensation → cumulus base/top
  4. large-scale subsidence (ICON W or a fixed v_sub) descends the profile,
     capping mixed-layer growth — the Figure-4 control

This is the bin-resolved, moist, AHD-weighted successor to the v0.1 bulk
encroachment model (`mixed_layer.py`), which is kept as the fast `--model bulk` path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from alptherm_icon.model import thermo as th
from alptherm_icon.regions.ahd import AHDProfile

# Eq (12) two-regime ΔT constants.
P0_W_M2 = 75.0
DELTA_T0_K = 0.5
# Eq (19) wind-reduction constant [(km/h)⁻²].
WIND_R = 1.65e-4
# Eq (18) fractional entrainment rate [1/m]. The plume mixes in environmental
# air as it rises, so the updraft drains buoyant energy as drag (2·μ·w² per dz)
# instead of converting all of it to vertical KE — this is what keeps modelled
# thermals at glider scale (a few m/s) rather than the raw √(2·CAPE).
ENTRAIN_ACCEL = 0.0025
ENTRAIN_SLOW = 0.008


def delta_t_regime(p_sens_w_m2: float, p0: float = P0_W_M2, dt0: float = DELTA_T0_K) -> float:
    """Eq (12): parcel super-temperature. Linear below P₀, saturated at ΔT₀ above."""
    if p_sens_w_m2 <= 0.0:
        return 0.0
    if p_sens_w_m2 >= p0:
        return dt0
    return dt0 * p_sens_w_m2 / p0


def parcel_mass(h_sens_j: float, delta_t_K: float) -> float:
    """Eq (13): m_p = H_sens / (c_p · ΔT)."""
    if delta_t_K <= 0.0 or h_sens_j <= 0.0:
        return 0.0
    return h_sens_j / (th.C_P * delta_t_K)


def wind_reduction(u_km_h: float, r: float = WIND_R) -> float:
    """Eq (19): f_kin = 1 − r·u², clamped to [0, 1] (energy multiplier)."""
    return float(np.clip(1.0 - r * u_km_h**2, 0.0, 1.0))


def entrainment_coeff(accelerating: bool) -> float:
    """Eq (18): entrainment is weaker while the plume accelerates, stronger while
    it decelerates (a plume that has stopped accelerating mixes out faster)."""
    return ENTRAIN_ACCEL if accelerating else ENTRAIN_SLOW


@dataclass
class LayerGrid:
    """Atmospheric column on 100 m layers with the region AHD attached."""

    z_center_m: np.ndarray  # layer mid-height [m ASL], ascending
    dz_m: float
    s_g_m2: np.ndarray  # heated terrain area per layer (AHD)
    v_a_m3: np.ndarray  # residual air volume per layer (AHD)
    theta_K: np.ndarray  # current potential temperature (state, mutated in place)
    q_kg_kg: np.ndarray  # current water-vapour mixing ratio (state)
    p_Pa: np.ndarray  # layer pressure (fixed, ISA)
    region_area_m2: float

    @property
    def T_K(self) -> np.ndarray:
        return np.asarray(th.temperature_from_theta(self.theta_K, self.p_Pa))


def build_grid(
    z_sounding_m: np.ndarray,
    T_sounding_K: np.ndarray,
    Td_sounding_K: np.ndarray,
    ahd: AHDProfile,
) -> LayerGrid:
    """Bin the morning sounding onto the AHD layers and attach S_G/V_a."""
    z_center = 0.5 * (ahd.z_bottom_m + ahd.z_top_m)
    dz = float(ahd.z_top_m[0] - ahd.z_bottom_m[0])
    p = np.asarray(th.standard_pressure(z_center), dtype=np.float64)
    T = np.interp(z_center, z_sounding_m, T_sounding_K)
    Td = np.interp(z_center, z_sounding_m, Td_sounding_K)
    e_hpa = np.asarray(th.vapor_pressure_from_dewpoint(Td))
    q = np.asarray(th.mixing_ratio(e_hpa, p), dtype=np.float64)
    theta = np.asarray(th.potential_temperature(T, p), dtype=np.float64)
    return LayerGrid(
        z_center_m=z_center,
        dz_m=dz,
        s_g_m2=np.asarray(ahd.s_g, dtype=np.float64),
        v_a_m3=np.asarray(ahd.v_a, dtype=np.float64),
        theta_K=theta,
        q_kg_kg=q,
        p_Pa=p,
        region_area_m2=float(ahd.region_area_m2),
    )


@dataclass
class ParcelAscent:
    """Result of lifting one surface parcel through the current profile."""

    top_idx: int  # index of the level of neutral buoyancy
    v_max_m_s: float  # peak updraft speed √(2·E/m)
    v_profile_m_s: np.ndarray  # updraft speed at each layer (0 outside the ascent)
    condensed: bool
    cloud_base_m: float  # NaN if dry
    cloud_top_m: float  # NaN if dry


def ascend_parcel(grid: LayerGrid, origin_idx: int, delta_t_K: float) -> ParcelAscent:
    """Eqs (15)–(17): lift a parcel from ``origin_idx`` and integrate buoyancy.

    The parcel starts ΔT warmer than its layer, rises dry-adiabatically until it
    reaches saturation (LCL), then moist-adiabatically with condensate loading.
    Two quantities are tracked independently along the ascent:

    * **buoyant extent** (eqs 15–16): cumulative buoyant energy ``e_cum`` rises
      while the parcel is positively buoyant and the ascent ends at the level of
      neutral buoyancy. This fixes the cloud base (LCL) and cloud top.
    * **updraft speed** (eqs 17–18): the entraining-plume momentum equation
      ``d(w²)/dz = 2·B − 2·μ·w²`` — buoyancy accelerates the plume, entrainment
      drains it as drag — which keeps the usable lift at glider scale. The plume
      may stall (w² ← 0) below the buoyant top; above that, v is 0.
    """
    n = grid.z_center_m.size
    v_profile = np.zeros(n, dtype=np.float64)
    T_env = grid.T_K
    T_p = float(T_env[origin_idx] + delta_t_K)
    q_p = float(grid.q_kg_kg[origin_idx])
    q_liquid = 0.0
    saturated = False
    e_cum = 0.0
    w2 = 0.0
    w2_prev = 0.0
    cloud_base = np.nan
    cloud_top = np.nan
    top_idx = origin_idx

    for k in range(origin_idx + 1, n):
        p_k = grid.p_Pa[k]
        # Lift the parcel from level k-1 to k.
        if not saturated:
            T_p = T_p * (p_k / grid.p_Pa[k - 1]) ** th.KAPPA  # dry adiabat, θ conserved
            q_sat = float(th.saturation_mixing_ratio(T_p, p_k))
            if q_p >= q_sat:
                saturated = True
                cloud_base = float(grid.z_center_m[k])
        if saturated:
            gamma_m = float(th.moist_adiabatic_lapse_rate(T_p, p_k))
            T_p = T_p - gamma_m * grid.dz_m
            q_sat = float(th.saturation_mixing_ratio(T_p, p_k))
            q_liquid += max(q_p - q_sat, 0.0)
            q_p = min(q_p, q_sat)

        rho_p = float(th.air_density(T_p, p_k, q_p, q_liquid))
        rho_f = float(th.air_density(T_env[k], p_k))
        buoyancy = th.G * (rho_f / rho_p - 1.0)  # eq (15) acceleration form

        # Buoyant extent: rise while cumulative buoyant energy stays positive.
        e_cum += buoyancy * grid.dz_m
        if e_cum <= 0.0 and k > origin_idx + 1:
            break
        top_idx = k
        if saturated:
            cloud_top = float(grid.z_center_m[k])

        # Usable updraft speed: entraining-plume momentum (may stall earlier).
        mu = entrainment_coeff(accelerating=w2 >= w2_prev)
        w2_prev = w2
        w2 = max(w2 + 2.0 * grid.dz_m * (buoyancy - mu * w2), 0.0)
        v_profile[k] = np.sqrt(w2)

    return ParcelAscent(
        top_idx=top_idx,
        v_max_m_s=float(v_profile.max()),
        v_profile_m_s=v_profile,
        condensed=bool(np.isfinite(cloud_base)),
        cloud_base_m=cloud_base,
        cloud_top_m=cloud_top,
    )


def heat_and_mix(grid: LayerGrid, p_sens_w_m2: float, dt_s: float) -> None:
    """Eq (11) + convective adjustment, in place.

    Each layer's air volume V_a is warmed by the sensible heat from the terrain
    area S_G at that height (the volume effect). Then a dry convective adjustment
    mixes any super-adiabatic stack to uniform θ — the mixed-layer growth.
    """
    if p_sens_w_m2 <= 0.0:
        return
    rho = np.asarray(th.air_density(grid.T_K, grid.p_Pa), dtype=np.float64)
    air_mass = rho * grid.v_a_m3  # kg per layer
    dq = p_sens_w_m2 * grid.s_g_m2 * dt_s  # J per layer
    with np.errstate(divide="ignore", invalid="ignore"):
        dtheta = np.where(air_mass > 0.0, dq / (th.C_P * air_mass), 0.0)
    grid.theta_K = grid.theta_K + dtheta

    # Dry convective adjustment: sweep up, enthalpy-conserving mixing of any
    # layer cooler (in θ) than the running mixed-layer value below it.
    theta = grid.theta_K
    mass = air_mass
    i = 0
    n = theta.size
    while i < n - 1:
        if theta[i + 1] < theta[i]:
            j = i + 1
            # Extend the mixed block while it stays super-adiabatic vs. its mean.
            while True:
                block = slice(i, j + 1)
                theta_bar = np.average(theta[block], weights=mass[block])
                if j + 1 < n and theta[j + 1] < theta_bar:
                    j += 1
                    continue
                theta[block] = theta_bar
                break
            i = 0  # restart: mixing can expose new instability below
        else:
            i += 1


def apply_subsidence(grid: LayerGrid, w_sub_m_s: float, dt_s: float) -> None:
    """Large-scale subsidence: descend the θ/q profile by w_sub·Δt (downward
    advection). Brings warmer air down from aloft, capping mixed-layer growth.
    ``w_sub_m_s`` > 0 means sinking (the Fig-4 control)."""
    if w_sub_m_s <= 0.0:
        return
    shift = w_sub_m_s * dt_s
    z = grid.z_center_m
    # θ(z) ← θ(z + shift): each level takes the value from `shift` metres higher.
    grid.theta_K = np.interp(z + shift, z, grid.theta_K, right=grid.theta_K[-1])
    grid.q_kg_kg = np.interp(z + shift, z, grid.q_kg_kg, right=grid.q_kg_kg[-1])


@dataclass
class DayStep:
    z_i_m: float  # mixed-layer top (mid-height of the well-mixed block)
    theta_surface_K: float
    v_max_m_s: float
    cloud_base_m: float
    cloud_top_m: float
    cloud_cover_octas: float


def mixed_layer_top(grid: LayerGrid, tol_K: float = 0.1) -> int:
    """Top index of the well-mixed (near-uniform θ) block above the surface."""
    theta = grid.theta_K
    k = 0
    while k + 1 < theta.size and theta[k + 1] - theta[0] <= tol_K:
        k += 1
    return k


# Forcing callable: (step, T_surface_air_K, Td_surface_K) -> (P_sens, P_lat) [W/m²].
ForcingFn = Callable[[int, float, float], tuple[float, float]]


def step(
    grid: LayerGrid,
    p_sens_w_m2: float,
    u_km_h: float,
    w_sub_m_s: float,
    dt_s: float,
) -> DayStep:
    """One Δt: heat+mix, then diagnose parcels/clouds, then subside."""
    heat_and_mix(grid, p_sens_w_m2, dt_s)

    z_i_idx = mixed_layer_top(grid)
    delta_t = delta_t_regime(p_sens_w_m2)
    f_kin = wind_reduction(u_km_h)

    v_max = 0.0
    cloud_base = np.nan
    cloud_top = np.nan
    cloudy_area = 0.0
    total_area = float(grid.s_g_m2.sum())
    if delta_t > 0.0:
        # Parcels originate from heated terrain across the mixed layer.
        for origin in range(0, z_i_idx + 1):
            if grid.s_g_m2[origin] <= 0.0:
                continue
            asc = ascend_parcel(grid, origin, delta_t)
            v_eff = asc.v_max_m_s * np.sqrt(f_kin)
            v_max = max(v_max, v_eff)
            if asc.condensed:
                cloudy_area += grid.s_g_m2[origin]
                cloud_base = (
                    asc.cloud_base_m
                    if not np.isfinite(cloud_base)
                    else min(cloud_base, asc.cloud_base_m)
                )
                cloud_top = (
                    asc.cloud_top_m
                    if not np.isfinite(cloud_top)
                    else max(cloud_top, asc.cloud_top_m)
                )
    octas = 8.0 * (cloudy_area / total_area) if total_area > 0.0 else 0.0

    apply_subsidence(grid, w_sub_m_s, dt_s)

    return DayStep(
        z_i_m=float(grid.z_center_m[z_i_idx]),
        theta_surface_K=float(grid.theta_K[0]),
        v_max_m_s=float(v_max),
        cloud_base_m=cloud_base,
        cloud_top_m=cloud_top,
        cloud_cover_octas=float(octas),
    )


def run_day(
    grid: LayerGrid,
    forcing: ForcingFn,
    n_steps: int,
    dt_s: float,
    u_km_h: float = 0.0,
    w_sub_m_s: float = 0.0,
) -> list[DayStep]:
    """Integrate the column forward ``n_steps`` × Δt.

    ``forcing`` returns (P_sens, P_lat) given the current surface air temperature
    and dewpoint, so the Liechti reference budget can track daytime heating; an
    ICON-driven run simply ignores its arguments and returns the de-averaged series.
    """
    steps: list[DayStep] = []
    for s in range(n_steps):
        T_surf = float(grid.T_K[0])
        e_hpa = grid.q_kg_kg[0] * grid.p_Pa[0] / 100.0 / (th.EPSILON + grid.q_kg_kg[0])
        # Surface dewpoint for the radiation humidity term (eq 6).
        Td_surface = float(th.dewpoint_from_vapor_pressure(e_hpa))
        p_sens, _p_lat = forcing(s, T_surf, Td_surface)
        steps.append(step(grid, p_sens, u_km_h, w_sub_m_s, dt_s))
    return steps
