"""Liechti & Neininger (1994) golden validation — Komp. C v0.3 parcel kernel.

Drives the parcel kernel with the paper's own inputs: the Table-1 morning
sounding + the empirical reference forcing (eqs 1–10). Two confidence tiers:

* **Qualitative (asserted):** the parcel physics must produce afternoon cumulus
  over the Voralpen valley and a cloud cover that does not *increase* with
  large-scale subsidence (the Figure-4 trend).
* **Quantitative (xfail):** exact reproduction of Table 2 (onset ≈ 13:00, base
  ≈ 1700 m) and the Figure-4 threshold (cover → 0 at 20 m/h). These need the
  *real* Voralpen AHD (this test uses a synthetic valley AHD) plus parameter
  calibration — tracked for the v0.4 tuning pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from alptherm_icon.model import forcing as f
from alptherm_icon.model import parcel as P
from alptherm_icon.model.reference import load_initial_sounding
from alptherm_icon.regions.ahd import AHDProfile

SOUNDING_CSV = "tests/fixtures/liechti1994/initial_sounding.csv"
LAT_DEG = 47.0
DAY_OF_YEAR = 135  # mid-May (Swiss Nationals, May 1993)
DT_S = 120.0
START_H, END_H = 7.0, 19.0


def _voralpen_ahd(a_region: float = 1.0e9) -> AHDProfile:
    """Synthetic Voralpen valley AHD (small low-level air volume = volume effect).

    Stand-in for the real Komp.-A AHD until that region is built; documented as
    synthetic so the quantitative tests stay xfail.
    """
    edges = np.arange(400.0, 5001.0, 100.0)
    zb, zt = edges[:-1], edges[1:]
    zc = 0.5 * (zb + zt)
    A = np.where(zc <= 2300, 0.03 * a_region + 0.97 * a_region * (zc - 400) / 1900.0, a_region)
    A = np.clip(A, 0.03 * a_region, a_region)
    v_a = A * 100.0
    s_g = np.clip(np.concatenate([[A[0]], np.diff(A)]), 1.0, None)
    s_g[zc > 2300] = 0.0
    return AHDProfile("voralpen_synth", zb, zt, s_g, v_a, a_region)


def _run(v_sub_m_per_h: float) -> tuple[np.ndarray, list[P.DayStep]]:
    snd = load_initial_sounding(SOUNDING_CSV)
    ahd = _voralpen_ahd()
    grid = P.build_grid(snd.z_m, snd.T_K, snd.Td_K, ahd)
    hours = START_H + np.arange(0, (END_H - START_H) * 3600.0 / DT_S) * DT_S / 3600.0
    sin_eps, sin_max = f.sin_elevation_series(LAT_DEG, DAY_OF_YEAR, hours)

    def forcing(s: int, T_surf: float, Td_surf: float) -> tuple[float, float]:
        rf = f.liechti_surface_flux(T_surf, Td_surf, float(snd.z_m[0]), sin_eps[s], sin_max)
        return rf.p_sens_w_m2, rf.p_lat_w_m2

    steps = P.run_day(grid, forcing, len(hours), DT_S, w_sub_m_s=v_sub_m_per_h / 3600.0)
    return hours, steps


# --- qualitative (must pass) ------------------------------------------------ #

def test_afternoon_cumulus_forms() -> None:
    hours, steps = _run(0.0)
    cloudy = np.array([np.isfinite(s.cloud_base_m) for s in steps])
    assert cloudy.any(), "no cumulus formed at all"
    onset_h = hours[np.argmax(cloudy)]
    assert 10.0 <= onset_h <= 17.0  # midday convective window


def test_cloud_base_is_physical() -> None:
    _, steps = _run(0.0)
    bases = np.array([s.cloud_base_m for s in steps])
    bases = bases[np.isfinite(bases)]
    assert bases.size > 0
    assert np.all((bases > 1200.0) & (bases < 3000.0))  # plausible Alpine Cu base band


def test_updraft_speeds_are_glider_scale() -> None:
    _, steps = _run(0.0)
    v_max = max(s.v_max_m_s for s in steps)
    assert 0.5 < v_max < 6.0  # m/s — thermals, not a hurricane


def test_fig4_cloud_cover_monotone_in_subsidence() -> None:
    """Headline trend: max cloud cover must not increase with subsidence rate."""
    octas = [max(s.cloud_cover_octas for s in _run(v)[1]) for v in (0.0, 5.0, 10.0, 20.0)]
    assert all(b <= a + 1e-9 for a, b in zip(octas, octas[1:])), octas


# --- quantitative (xfail until real AHD + calibration, v0.4) ---------------- #

@pytest.mark.xfail(reason="exact Table-2 numerics need the real Voralpen AHD + tuning", strict=False)
def test_table2_onset_and_base() -> None:
    hours, steps = _run(0.0)
    cloudy = np.array([np.isfinite(s.cloud_base_m) for s in steps])
    onset_h = hours[np.argmax(cloudy)]
    first_base = next(s.cloud_base_m for s in steps if np.isfinite(s.cloud_base_m))
    assert onset_h == pytest.approx(13.0, abs=0.5)
    assert first_base == pytest.approx(1700.0, abs=200.0)


@pytest.mark.xfail(reason="Fig-4 threshold (cover→0 at 20 m/h) needs calibration", strict=False)
def test_fig4_vanishes_at_20() -> None:
    octas_20 = max(s.cloud_cover_octas for s in _run(20.0)[1])
    assert octas_20 == pytest.approx(0.0, abs=0.5)
