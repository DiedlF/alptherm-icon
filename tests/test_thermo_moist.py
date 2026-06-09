"""Moist-thermodynamics tests for Komp. C v0.3 — values vs. textbook references."""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model import thermo as th


def test_saturation_vapor_pressure_reference_points() -> None:
    # e_s(0 °C) = 6.112 hPa by construction; e_s(20 °C) ≈ 23.4 hPa (textbook).
    assert th.saturation_vapor_pressure(273.15) == pytest.approx(6.112, rel=1e-3)
    assert th.saturation_vapor_pressure(293.15) == pytest.approx(23.4, rel=0.02)
    # Monotone increasing in T.
    temps = np.array([260.0, 273.15, 290.0, 300.0])
    assert np.all(np.diff(th.saturation_vapor_pressure(temps)) > 0)


def test_saturation_mixing_ratio_at_20C() -> None:
    # r_s(20 °C, 1000 hPa) ≈ 14.9 g/kg.
    r_s = th.saturation_mixing_ratio(293.15, 100000.0)
    assert r_s == pytest.approx(0.0149, rel=0.02)


def test_relative_humidity_saturated_and_dry() -> None:
    assert th.relative_humidity(293.15, 293.15) == pytest.approx(1.0)
    rh = th.relative_humidity(293.15, 283.15)  # 10 K depression
    assert 0.4 < rh < 0.6  # ~0.52


def test_lcl_height_espy_rule() -> None:
    # 10 K dewpoint depression → 1250 m above the start height.
    assert th.lcl_height(283.15, 273.15, 0.0) == pytest.approx(1250.0)
    assert th.lcl_height(283.15, 273.15, 500.0) == pytest.approx(1750.0)
    # Saturated parcel condenses immediately (no negative LCL).
    assert th.lcl_height(283.15, 283.15, 400.0) == pytest.approx(400.0)


def test_virtual_temperature_exceeds_dry_and_reduces_to_T() -> None:
    assert th.virtual_temperature(293.15, 0.0) == pytest.approx(293.15)
    tv = th.virtual_temperature(293.15, 0.0149)
    assert tv > 293.15 and tv == pytest.approx(295.8, rel=0.01)


def test_density_temperature_condensate_loading_lowers_buoyancy() -> None:
    # Adding liquid water (loading) lowers the density temperature → denser parcel.
    t_v = th.density_temperature(293.15, 0.0149, 0.0)
    t_rho_loaded = th.density_temperature(293.15, 0.0149, 0.002)
    assert t_rho_loaded < t_v


def test_air_density_isa_sea_level() -> None:
    # Dry ISA sea level ≈ 1.225 kg/m³.
    assert th.air_density(288.15, 101325.0) == pytest.approx(1.225, rel=2e-3)


def test_moist_lapse_rate_below_dry_and_approaches_it_when_cold() -> None:
    dry = th.G / th.C_P  # 9.76 K/km
    warm = th.moist_adiabatic_lapse_rate(295.0, 100000.0)
    cold = th.moist_adiabatic_lapse_rate(233.0, 50000.0)
    assert 0.003 < warm < dry  # latent release softens the warm rate (~4–5 K/km)
    assert cold == pytest.approx(dry, rel=0.05)  # negligible moisture when cold
