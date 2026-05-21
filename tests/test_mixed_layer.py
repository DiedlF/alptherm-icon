"""Tests for the bulk mixed-layer encroachment model (Komp. C v0.1)."""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model.mixed_layer import (
    cbl_top_from_cumulative_heat,
    evolve_mixed_layer,
)
from alptherm_icon.model.thermo import C_P


def _linear_theta(z: np.ndarray, theta_0: float, dtheta_dz: float) -> np.ndarray:
    """Linear θ(z) = θ_0 + γ·z. γ > 0 = stable."""
    return theta_0 + dtheta_dz * z


def test_cbl_top_no_heat_returns_surface() -> None:
    z = np.linspace(0, 3000, 31)
    theta = _linear_theta(z, 290.0, 0.004)
    z_i, theta_m = cbl_top_from_cumulative_heat(theta, z, h_cum_j_m2=0.0)
    assert z_i == pytest.approx(z[0])
    assert theta_m == pytest.approx(theta[0])


def test_cbl_top_linear_profile_analytical() -> None:
    """For a linear θ profile θ_0 + γ·z and constant ρ, encroachment yields

        H_cum = c_p · ρ · γ · z_i² / 2  =>  z_i = sqrt(2·H_cum / (c_p·ρ·γ))

    Verify numerical answer matches the analytical one within 1%.
    """
    z = np.linspace(0, 3000, 301)
    gamma = 0.004  # K/m
    theta = _linear_theta(z, 290.0, gamma)
    rho = 1.0
    h_cum = 5.0e5  # 0.5 MJ/m²
    z_i_analytical = np.sqrt(2 * h_cum / (C_P * rho * gamma))
    z_i, theta_m = cbl_top_from_cumulative_heat(theta, z, h_cum, rho_kg_m3=rho)
    assert z_i == pytest.approx(z_i_analytical, rel=0.01)
    assert theta_m == pytest.approx(_linear_theta(np.array([z_i]), 290.0, gamma)[0], rel=1e-3)


def test_cbl_top_grows_with_heat() -> None:
    z = np.linspace(0, 3000, 301)
    theta = _linear_theta(z, 290.0, 0.004)
    z_small, _ = cbl_top_from_cumulative_heat(theta, z, 1.0e5)
    z_large, _ = cbl_top_from_cumulative_heat(theta, z, 1.0e6)
    assert z_small < z_large


def test_cbl_top_saturates_at_profile_top() -> None:
    z = np.linspace(0, 3000, 301)
    theta = _linear_theta(z, 290.0, 0.004)
    z_i, _ = cbl_top_from_cumulative_heat(theta, z, h_cum_j_m2=1.0e12)
    assert z_i == pytest.approx(z[-1])


def test_cbl_top_rejects_non_monotone_z() -> None:
    z = np.array([0.0, 100.0, 50.0, 200.0])  # not monotone
    theta = np.array([290.0, 290.5, 291.0, 291.5])
    with pytest.raises(ValueError, match="strictly ascending"):
        cbl_top_from_cumulative_heat(theta, z, 1.0e5)


def test_evolve_mixed_layer_monotone_under_constant_heating() -> None:
    """Constant positive surface flux → z_i and θ_m monotone non-decreasing."""
    z = np.linspace(0, 3000, 301)
    theta = _linear_theta(z, 290.0, 0.004)
    fluxes = np.full(12, 150.0)  # 150 W/m² for 12 hours
    steps = evolve_mixed_layer(theta, z, fluxes, dt_s=3600.0)
    z_is = np.array([s.z_i_m for s in steps])
    theta_ms = np.array([s.theta_m_K for s in steps])
    h_cums = np.array([s.h_cum_j_m2 for s in steps])
    assert np.all(np.diff(z_is) > 0)
    assert np.all(np.diff(theta_ms) > 0)
    assert np.all(np.diff(h_cums) > 0)
    # w* should be positive every step (flux > 0).
    assert all(s.w_star_m_s > 0 for s in steps)


def test_evolve_mixed_layer_freezes_under_zero_flux() -> None:
    """Zero flux → z_i stays at surface, θ_m stays at θ(surface), w* = 0."""
    z = np.linspace(0, 3000, 301)
    theta = _linear_theta(z, 290.0, 0.004)
    fluxes = np.zeros(6)
    steps = evolve_mixed_layer(theta, z, fluxes, dt_s=3600.0)
    for s in steps:
        assert s.z_i_m == pytest.approx(z[0])
        assert s.theta_m_K == pytest.approx(theta[0])
        assert s.w_star_m_s == 0.0
