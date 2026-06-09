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
# Eq (18) momentum-drag coefficient [1/m]. The plume mixes in environmental air
# as it rises, draining its vertical KE as drag (2·μ·w² per dz) instead of
# converting all buoyancy to speed — this keeps modelled thermals at glider scale
# (a few m/s) rather than the raw √(2·CAPE).
ENTRAIN_ACCEL = 0.0025
ENTRAIN_SLOW = 0.008
# Thermodynamic entrainment [1/m]: the fractional rate at which the parcel's
# T/q relax toward the environment (mass mixing). Much smaller than the momentum
# drag — typical cumulus is ~0.3/km — so the parcel still reaches its LCL, but
# the dry-air dilution caps its buoyant top at a realistic level of neutral
# buoyancy instead of an undiluted moist adiabat running to the domain top.
THERMO_ENTRAIN = 0.0004
# Cumulus trigger calibration (tuned on the Liechti Voralpen worked example):
#  - a real convecting CBL must be at least this deep before cumulus can form
#    (excludes the shallow, moist dawn layer whose LCL is trivially low);
#  - the mixed-layer top is allowed this much entrainment-zone overshoot when
#    testing whether it has reached the LCL (thermals overshoot the well-mixed top);
#  - cumulus-layer depth [m] that counts as one octa of cover.
MIN_CBL_DEPTH_M = 500.0
CLOUD_TRIGGER_OVERSHOOT_M = 200.0
CLOUD_OCTA_DEPTH_M = 800.0


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
        mu_e = entrainment_coeff(accelerating=w2 >= w2_prev)
        w2_prev = w2

        # Lift the parcel from level k-1 to k (dry adiabat → moist above LCL).
        if not saturated:
            T_p = T_p * (p_k / grid.p_Pa[k - 1]) ** th.KAPPA  # θ conserved
            if q_p >= th.saturation_mixing_ratio(T_p, p_k):
                saturated = True
                cloud_base = float(grid.z_center_m[k])
        if saturated:
            T_p -= float(th.moist_adiabatic_lapse_rate(T_p, p_k)) * grid.dz_m
            q_sat = float(th.saturation_mixing_ratio(T_p, p_k))
            q_liquid += max(q_p - q_sat, 0.0)
            q_p = min(q_p, q_sat)

        # Lateral entrainment (eq 18): mix the parcel toward the environment,
        # draining its buoyancy excess so the buoyant top is capped realistically
        # (not run to the domain top on an undiluted moist adiabat). Uses the
        # weaker thermodynamic rate so the parcel still reaches its LCL.
        frac = min(THERMO_ENTRAIN * grid.dz_m, 1.0)
        T_p += frac * (float(T_env[k]) - T_p)
        q_p += frac * (float(grid.q_kg_kg[k]) - q_p)
        q_liquid *= 1.0 - frac

        rho_p = float(th.air_density(T_p, p_k, q_p, q_liquid))
        rho_f = float(th.air_density(T_env[k], p_k))
        buoyancy = th.G * (rho_f / rho_p - 1.0)  # eq (15) acceleration form

        # Buoyant extent (eqs 15–16): rise while cumulative buoyant energy > 0.
        e_cum += buoyancy * grid.dz_m
        if e_cum <= 0.0 and k > origin_idx + 1:
            break
        top_idx = k
        if saturated:
            cloud_top = float(grid.z_center_m[k])

        # Usable updraft speed (eqs 17–18): entraining-plume momentum.
        w2 = max(w2 + 2.0 * grid.dz_m * (buoyancy - mu_e * w2), 0.0)
        v_profile[k] = np.sqrt(w2)

    return ParcelAscent(
        top_idx=top_idx,
        v_max_m_s=float(v_profile.max()),
        v_profile_m_s=v_profile,
        condensed=bool(np.isfinite(cloud_base)),
        cloud_base_m=cloud_base,
        cloud_top_m=cloud_top,
    )


def heat_and_mix(
    grid: LayerGrid, p_sens_w_m2: float, p_lat_w_m2: float, dt_s: float
) -> None:
    """Eq (11) heating + latent moistening + convective adjustment, in place.

    Each layer's air volume V_a is warmed by the sensible heat *and* moistened by
    the latent heat (evaporation) from the terrain area S_G at that height — the
    volume effect. The latent input raises the mixed-layer dewpoint through the
    morning (Liechti Table 2: Td 6→10 °C), which lowers the LCL so cumulus can
    form. A convective adjustment then mixes any super-adiabatic stack to uniform
    θ *and* q — the well-mixed growing CBL.
    """
    if p_sens_w_m2 <= 0.0:
        return
    rho = np.asarray(th.air_density(grid.T_K, grid.p_Pa), dtype=np.float64)
    air_mass = rho * grid.v_a_m3  # kg per layer
    with np.errstate(divide="ignore", invalid="ignore"):
        dtheta = np.where(air_mass > 0.0, p_sens_w_m2 * grid.s_g_m2 * dt_s / (th.C_P * air_mass), 0.0)
        # Latent flux → evaporated water mass (eq 14) → mixing-ratio increment.
        dr = np.where(
            air_mass > 0.0,
            max(p_lat_w_m2, 0.0) * grid.s_g_m2 * dt_s / (th.L_V * air_mass),
            0.0,
        )
    grid.theta_K = grid.theta_K + dtheta
    grid.q_kg_kg = grid.q_kg_kg + dr

    # Convective adjustment: sweep up, enthalpy-conserving mixing of any layer
    # cooler (in θ) than the running mixed-layer value below it; q is mixed over
    # the same block (the CBL is well-mixed in both θ and moisture).
    theta = grid.theta_K
    q = grid.q_kg_kg
    mass = air_mass
    i = 0
    n = theta.size
    while i < n - 1:
        if theta[i + 1] < theta[i]:
            j = i + 1
            while True:
                block = slice(i, j + 1)
                theta_bar = np.average(theta[block], weights=mass[block])
                if j + 1 < n and theta[j + 1] < theta_bar:
                    j += 1
                    continue
                theta[block] = theta_bar
                q[block] = np.average(q[block], weights=mass[block])
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
    v_profile_m_s: np.ndarray  # updraft speed per layer (the v(z,t) row)


def mixed_layer_top(grid: LayerGrid, tol_K: float = 0.1) -> int:
    """Top index of the well-mixed (near-uniform θ) block above the surface."""
    theta = grid.theta_K
    k = 0
    while k + 1 < theta.size and theta[k + 1] - theta[0] <= tol_K:
        k += 1
    return k


def surface_lcl_m(grid: LayerGrid) -> float:
    """Lifting condensation level [m ASL] of the (mixed-layer) surface air."""
    T = float(grid.T_K[0])
    q = float(grid.q_kg_kg[0])
    e_hpa = q * float(grid.p_Pa[0]) / 100.0 / (th.EPSILON + q)
    Td = float(th.dewpoint_from_vapor_pressure(e_hpa))
    return float(th.lcl_height(T, Td, grid.z_center_m[0]))


# Forcing callable: (step, T_surface_air_K, Td_surface_K) -> (P_sens, P_lat) [W/m²].
ForcingFn = Callable[[int, float, float], tuple[float, float]]


def step(
    grid: LayerGrid,
    p_sens_w_m2: float,
    p_lat_w_m2: float,
    u_km_h: float,
    w_sub_m_s: float,
    dt_s: float,
) -> DayStep:
    """One Δt: heat+mix, diagnose updrafts + cumulus, then subside.

    Cumulus is gated on mixed-layer maturity: clouds form only once the
    mixed-layer top ``z_i`` has grown up to the surface parcel's LCL. Below that
    the day is "blue" (dry thermals); large-scale subsidence that holds ``z_i``
    under the LCL therefore suppresses cloud entirely — the Figure-4 control.
    """
    heat_and_mix(grid, p_sens_w_m2, p_lat_w_m2, dt_s)

    z_i_idx = mixed_layer_top(grid)
    z_i = float(grid.z_center_m[z_i_idx])
    delta_t = delta_t_regime(p_sens_w_m2)
    f_kin = wind_reduction(u_km_h)

    # Updraft-speed field: max over AHD-weighted parcel ascents (the v(z,t) row).
    v_profile = np.zeros(grid.z_center_m.size, dtype=np.float64)
    asc_surface = None
    if delta_t > 0.0:
        for origin in range(0, z_i_idx + 1):
            if grid.s_g_m2[origin] <= 0.0:
                continue
            asc = ascend_parcel(grid, origin, delta_t)
            v_profile = np.maximum(v_profile, asc.v_profile_m_s)
            if origin == 0:
                asc_surface = asc
        v_profile *= np.sqrt(f_kin)
    v_max = float(v_profile.max())

    # Cumulus forms when an actively convecting CBL (≥ MIN_CBL_DEPTH_M) has grown
    # up to its LCL — allowing the entrainment-zone overshoot. Cloud base is that
    # mixed-layer LCL; the depth comes from the surface parcel's moist ascent.
    cloud_base = np.nan
    cloud_top = np.nan
    octas = 0.0
    cbl_depth = z_i - float(grid.z_center_m[0])
    lcl = surface_lcl_m(grid)
    if (
        asc_surface is not None
        and asc_surface.condensed
        and cbl_depth >= MIN_CBL_DEPTH_M
        and z_i + CLOUD_TRIGGER_OVERSHOOT_M >= lcl
    ):
        cloud_base = lcl
        cloud_top = asc_surface.cloud_top_m
        depth = max((cloud_top - cloud_base) if np.isfinite(cloud_top) else 0.0, 0.0)
        octas = float(np.clip(round(depth / CLOUD_OCTA_DEPTH_M), 0, 8))

    apply_subsidence(grid, w_sub_m_s, dt_s)

    return DayStep(
        z_i_m=z_i,
        theta_surface_K=float(grid.theta_K[0]),
        v_max_m_s=v_max,
        cloud_base_m=cloud_base,
        cloud_top_m=cloud_top,
        cloud_cover_octas=float(octas),
        v_profile_m_s=v_profile,
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
        p_sens, p_lat = forcing(s, T_surf, Td_surface)
        steps.append(step(grid, p_sens, p_lat, u_km_h, w_sub_m_s, dt_s))
    return steps
