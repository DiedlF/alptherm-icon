"""Component tests for the bin-wise parcel kernel (Komp. C v0.3).

These validate the individual physics pieces (eqs 11–19) and the column
mechanics — not the full Liechti calibration, which lives in
``test_liechti_golden.py`` and needs the real region AHD.
"""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model import parcel as P
from alptherm_icon.model import thermo as th
from alptherm_icon.regions.ahd import AHDProfile


def _valley_ahd(a_region: float = 1.0e9, z_top: float = 5000.0) -> AHDProfile:
    """Synthetic valley AHD: small air volume low down (the volume effect)."""
    edges = np.arange(400.0, z_top + 1.0, 100.0)
    zb, zt = edges[:-1], edges[1:]
    zc = 0.5 * (zb + zt)
    A = np.where(zc <= 2300, 0.03 * a_region + 0.97 * a_region * (zc - 400) / 1900.0, a_region)
    A = np.clip(A, 0.03 * a_region, a_region)
    v_a = A * 100.0
    s_g = np.clip(np.concatenate([[A[0]], np.diff(A)]), 1.0, None)
    s_g[zc > 2300] = 0.0
    return AHDProfile("valley_test", zb, zt, s_g, v_a, a_region)


def _linear_grid(dtheta_dz: float, q0: float = 0.0, surface_T: float = 290.0) -> P.LayerGrid:
    """A grid with a linear θ(z) and uniform vapour, for ascent tests."""
    edges = np.arange(400.0, 5001.0, 100.0)
    zb, zt = edges[:-1], edges[1:]
    zc = 0.5 * (zb + zt)
    p = np.asarray(th.standard_pressure(zc), dtype=np.float64)
    theta0 = th.potential_temperature(surface_T, p[0])
    theta = theta0 + dtheta_dz * (zc - zc[0])
    q = np.full(zc.size, q0)
    # Flat plain: all heated terrain sits in the surface bin (heating from below);
    # V_a = footprint × 100 m per slab.
    s_g = np.full(zc.size, 1.0)
    s_g[0] = 1e6
    return P.LayerGrid(
        z_center_m=zc, dz_m=100.0,
        s_g_m2=s_g, v_a_m3=np.full(zc.size, 1e8),
        theta_K=theta, q_kg_kg=q, p_Pa=p, region_area_m2=1e6,
    )


# --- small equations -------------------------------------------------------- #

def test_delta_t_regime_two_branches() -> None:
    assert P.delta_t_regime(0.0) == 0.0
    assert P.delta_t_regime(37.5) == pytest.approx(0.25)  # linear, half of P0
    assert P.delta_t_regime(75.0) == pytest.approx(0.5)  # saturates at ΔT0
    assert P.delta_t_regime(300.0) == pytest.approx(0.5)  # stays saturated


def test_parcel_mass() -> None:
    assert P.parcel_mass(0.0, 0.5) == 0.0
    assert P.parcel_mass(th.C_P * 0.5 * 1000.0, 0.5) == pytest.approx(1000.0)


def test_wind_reduction_clamped() -> None:
    assert P.wind_reduction(0.0) == 1.0
    assert 0.0 < P.wind_reduction(50.0) < 1.0
    assert P.wind_reduction(1000.0) == 0.0  # clamped, never negative


# --- parcel ascent ---------------------------------------------------------- #

def test_ascent_dry_unstable_rises_with_positive_velocity() -> None:
    grid = _linear_grid(dtheta_dz=0.001, q0=0.0)  # weakly stable
    asc = P.ascend_parcel(grid, origin_idx=0, delta_t_K=2.0)
    assert asc.top_idx > 0
    assert asc.v_max_m_s > 0.0
    assert not asc.condensed  # bone dry → no cloud


def test_ascent_stable_profile_limits_height() -> None:
    weak = P.ascend_parcel(_linear_grid(0.002), 0, 1.0)
    strong = P.ascend_parcel(_linear_grid(0.012), 0, 1.0)  # strong inversion
    assert strong.top_idx <= weak.top_idx


def test_ascent_moist_parcel_condenses() -> None:
    # High surface humidity → parcel saturates while still buoyant → cloud base set.
    grid = _linear_grid(dtheta_dz=0.001, q0=0.011, surface_T=292.0)
    asc = P.ascend_parcel(grid, origin_idx=0, delta_t_K=2.0)
    assert asc.condensed
    assert np.isfinite(asc.cloud_base_m)
    assert asc.cloud_top_m >= asc.cloud_base_m


# --- column mechanics ------------------------------------------------------- #

def test_heat_and_mix_grows_mixed_layer() -> None:
    grid = _linear_grid(dtheta_dz=0.004)
    z0 = P.mixed_layer_top(grid)
    for _ in range(60):
        P.heat_and_mix(grid, p_sens_w_m2=150.0, dt_s=120.0)
    assert P.mixed_layer_top(grid) > z0


def test_heat_and_mix_noop_without_flux() -> None:
    grid = _linear_grid(dtheta_dz=0.004)
    theta_before = grid.theta_K.copy()
    P.heat_and_mix(grid, p_sens_w_m2=0.0, dt_s=120.0)
    assert np.allclose(grid.theta_K, theta_before)


def test_subsidence_warms_and_is_directional() -> None:
    grid = _linear_grid(dtheta_dz=0.004)
    theta_before = grid.theta_K.copy()
    P.apply_subsidence(grid, w_sub_m_s=0.05, dt_s=120.0)
    # Descending brings higher-θ air down → low-level θ increases (or holds).
    assert np.all(grid.theta_K >= theta_before - 1e-9)
    assert grid.theta_K[5] > theta_before[5]
    # Zero subsidence is a no-op.
    grid2 = _linear_grid(dtheta_dz=0.004)
    t2 = grid2.theta_K.copy()
    P.apply_subsidence(grid2, 0.0, 120.0)
    assert np.allclose(grid2.theta_K, t2)


def test_subsidence_suppresses_mixed_layer_growth() -> None:
    """More subsidence → shallower mixed layer for the same heating (Fig-4 trend)."""
    ahd = _valley_ahd()
    from alptherm_icon.model.reference import load_initial_sounding

    snd = load_initial_sounding("tests/fixtures/liechti1994/initial_sounding.csv")

    def forcing(_s: int, _T: float, _Td: float) -> tuple[float, float]:
        return 150.0, 200.0

    z_i_tops = []
    for v_sub in (0.0, 20.0 / 3600.0):
        grid = P.build_grid(snd.z_m, snd.T_K, snd.Td_K, ahd)
        steps = P.run_day(grid, forcing, n_steps=200, dt_s=120.0, w_sub_m_s=v_sub)
        z_i_tops.append(max(s.z_i_m for s in steps))
    assert z_i_tops[1] <= z_i_tops[0]  # subsidence does not deepen the CBL
