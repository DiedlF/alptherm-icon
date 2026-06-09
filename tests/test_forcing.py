"""Tests for surface-flux forcing (Komp. C v0.2).

ICON surface fluxes are means-since-init; the kernel needs interval-mean flux.
These cover the de-averaging transform, the DWD downward-positive sign flip,
and the edge cases (night, NaN gaps, malformed lead axes).
"""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model import forcing

HOUR = 3600.0


def test_deaverage_constant_flux_recovers_constant() -> None:
    """If the instantaneous flux is constant F, the mean-since-init is also F,
    and de-averaging must return F for every interval."""
    lead = np.arange(0, 6) * HOUR
    mean = np.full(6, 200.0)
    flux = forcing.deaverage_since_init(mean, lead)
    assert flux[0] == 0.0  # no interval precedes init
    assert np.allclose(flux[1:], 200.0)


def test_deaverage_ramp_matches_analytic() -> None:
    """For F(t) = a·t (linear ramp from 0), the mean-since-init is a·t/2, and
    the interval-mean over (t_{i-1}, t_i] is a·(t_i + t_{i-1})/2 — the midpoint."""
    a = 1e-2  # W/m² per second
    lead = np.arange(0, 6) * HOUR
    mean = 0.5 * a * lead  # ∫₀ᵗ a·t' dt' / t = a·t/2
    flux = forcing.deaverage_since_init(mean, lead)
    expected = a * 0.5 * (lead[1:] + lead[:-1])
    assert np.allclose(flux[1:], expected)


def test_deaverage_real_asob_s_curve() -> None:
    """Golden case from the archived inntal_steinberge run: de-averaging the
    mean-since-init ASOB_S yields a physical diurnal net-SW curve peaking near
    solar noon (~570 W/m²), well above the lagging running mean (~250)."""
    # lead hours 0..15, mean-since-init ASOB_S [W/m²] (from data/icon archive).
    mean = np.array(
        [0.0, 0.0, 0.0, 0.0, 0.93, 14.00, 43.97, 80.94, 119.07,
         156.27, 193.30, 225.73, 254.26, 278.57, 295.52, 304.00]
    )
    lead = np.arange(mean.size) * HOUR
    flux = forcing.deaverage_since_init(mean, lead)
    # Night is zero; midday peak is far above the running mean and physical.
    assert np.allclose(flux[:4], 0.0)
    peak = flux.max()
    assert 540.0 < peak < 600.0
    assert peak > mean.max()  # de-averaged peak exceeds the running mean
    # Peak occurs around solar noon (lead 12–13 h), not at the last lead.
    assert 12 <= int(flux.argmax()) <= 13


def test_turbulent_flux_from_icon_flips_sign() -> None:
    """DWD ASHFL_S is downward-positive; an upward (CBL-heating) flux is
    negative, so the model-positive sensible flux is −ASHFL_S."""
    lead = np.arange(0, 4) * HOUR
    # mean-since-init that de-averages to a steady downward-positive −150 W/m²
    # (i.e. 150 W/m² upward into the CBL).
    ashfl_mean = np.full(4, -150.0)
    p_sens = forcing.turbulent_flux_from_icon(ashfl_mean, lead)
    assert np.allclose(p_sens[1:], 150.0)  # positive = heating the CBL


def test_proxy_is_deaveraged_then_scaled() -> None:
    lead = np.arange(0, 4) * HOUR
    asob_mean = np.full(4, 400.0)
    p_sens = forcing.sensible_flux_proxy(asob_mean, lead, fraction=0.3)
    assert np.allclose(p_sens[1:], 120.0)


def test_nan_gap_propagates_but_does_not_crash() -> None:
    lead = np.arange(0, 5) * HOUR
    mean = np.array([0.0, 100.0, np.nan, 300.0, 400.0])
    flux = forcing.deaverage_since_init(mean, lead)
    assert np.isnan(flux[2]) and np.isnan(flux[3])  # the gap and the step after
    assert np.isfinite(flux[1]) and np.isfinite(flux[4])


def test_deaverage_rejects_nonzero_first_lead() -> None:
    with pytest.raises(ValueError, match="must be 0"):
        forcing.deaverage_since_init(np.array([1.0, 2.0]), np.array([HOUR, 2 * HOUR]))


def test_deaverage_rejects_non_ascending_lead() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        forcing.deaverage_since_init(
            np.array([0.0, 1.0, 2.0]), np.array([0.0, 2 * HOUR, HOUR])
        )
