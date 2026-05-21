"""Tests for the thermodynamic helpers (Komp. C)."""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model.thermo import (
    ISA_P0,
    ISA_T0,
    P_0,
    potential_temperature,
    standard_pressure,
    temperature_from_theta,
    w_star,
)


def test_standard_pressure_at_sea_level() -> None:
    assert float(standard_pressure(0.0)) == pytest.approx(ISA_P0, rel=1e-6)


def test_standard_pressure_at_5500m_about_half() -> None:
    """ISA half-pressure altitude ≈ 5,486 m."""
    p_5500 = float(standard_pressure(5500.0))
    assert 0.49 * ISA_P0 < p_5500 < 0.52 * ISA_P0


def test_potential_temperature_identity_at_p0() -> None:
    assert float(potential_temperature(ISA_T0, P_0)) == pytest.approx(ISA_T0)


def test_potential_temperature_increases_aloft() -> None:
    """A standard-atmosphere column has θ increasing with height (always stable)."""
    zs = np.linspace(0, 10000, 11)
    ps = standard_pressure(zs)
    ts = ISA_T0 - 0.0065 * zs
    thetas = potential_temperature(ts, ps)
    assert np.all(np.diff(thetas) > 0)


def test_theta_T_round_trip() -> None:
    p = 70000.0
    T_orig = 270.0
    theta = potential_temperature(T_orig, p)
    T_back = temperature_from_theta(theta, p)
    assert float(T_back) == pytest.approx(T_orig, rel=1e-12)


def test_w_star_zero_when_flux_nonpositive() -> None:
    assert w_star(0.0, 1000.0, 300.0) == 0.0
    assert w_star(-50.0, 1000.0, 300.0) == 0.0


def test_w_star_typical_magnitude() -> None:
    """Typical strong-convection conditions yield ~2 m/s."""
    w = w_star(200.0, 1500.0, 300.0)  # 200 W/m² surface flux, z_i=1500m, θ=300K
    assert 1.5 < w < 2.5


def test_w_star_scales_as_cube_root() -> None:
    """Doubling z_i (with other inputs fixed) scales w* by 2^(1/3)."""
    w1 = w_star(100.0, 1000.0, 290.0)
    w2 = w_star(100.0, 2000.0, 290.0)
    assert w2 / w1 == pytest.approx(2.0 ** (1.0 / 3.0), rel=1e-12)
