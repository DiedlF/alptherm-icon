"""Liechti & Neininger (1994) golden validation — Komp. C parcel kernel (v0.4).

Drives the parcel kernel with the paper's own inputs: the Table-1 morning
sounding + the empirical reference forcing (eqs 1–10), on the **real** Voralpen
AHD (built from the Alpine DEM over the Swiss pre-Alps; committed as a fixture).

Acceptance bar (v0.4 decision — "Fig-4 threshold first"):
* **Figure 4** (headline M3 test): max cloud cover monotone non-increasing in the
  subsidence rate AND ≈0 by 20 m/h.
* **Table 2** (broad bands): cumulus onset in the early afternoon, base 1500–2500 m
  — bands, not exact cells, given the 1994 scan's lower-confidence cloud values.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from alptherm_icon.model import forcing as f
from alptherm_icon.model import parcel as P
from alptherm_icon.model.reference import load_initial_sounding
from alptherm_icon.regions.ahd import AHDProfile

SOUNDING_CSV = "tests/fixtures/liechti1994/initial_sounding.csv"
VORALPEN_AHD = "tests/fixtures/liechti1994/voralpen_ahd.nc"
LAT_DEG = 47.0
DAY_OF_YEAR = 135  # mid-May (Swiss Nationals, May 1993)
DT_S = 120.0
START_H, END_H = 7.0, 19.0


def _voralpen_ahd() -> AHDProfile:
    """Load the real Voralpen AHD fixture (Swiss pre-Alps, from the Alpine DEM)."""
    ds = xr.open_dataset(VORALPEN_AHD)
    return AHDProfile(
        region_name="voralpen_liechti",
        z_bottom_m=ds["z_bottom"].values,
        z_top_m=ds["z_top"].values,
        s_g=ds["s_g"].values,
        v_a=ds["v_a"].values,
        region_area_m2=float(ds.attrs["region_area_m2"]),
    )


def _run(v_sub_m_per_h: float) -> tuple[np.ndarray, list[P.DayStep]]:
    snd = load_initial_sounding(SOUNDING_CSV)
    grid = P.build_grid(snd.z_m, snd.T_K, snd.Td_K, _voralpen_ahd())
    hours = START_H + np.arange(0, (END_H - START_H) * 3600.0 / DT_S) * DT_S / 3600.0
    sin_eps, sin_max = f.sin_elevation_series(LAT_DEG, DAY_OF_YEAR, hours)

    def forcing(s: int, T_surf: float, Td_surf: float) -> tuple[float, float]:
        rf = f.liechti_surface_flux(T_surf, Td_surf, float(snd.z_m[0]), sin_eps[s], sin_max)
        return rf.p_sens_w_m2, rf.p_lat_w_m2

    steps = P.run_day(grid, forcing, len(hours), DT_S, w_sub_m_s=v_sub_m_per_h / 3600.0)
    return hours, steps


def _max_octas(v_sub: float) -> float:
    return max(s.cloud_cover_octas for s in _run(v_sub)[1])


# --- Figure 4: the headline subsidence-sensitivity threshold ---------------- #

def test_fig4_cloud_cover_monotone_in_subsidence() -> None:
    octas = [_max_octas(v) for v in (0.0, 5.0, 10.0, 20.0)]
    assert all(b <= a + 1e-9 for a, b in zip(octas, octas[1:])), octas


def test_fig4_vanishes_by_20_m_per_h() -> None:
    """Convective cloud must be gone by 20 m/h subsidence (the paper's threshold)."""
    assert _max_octas(20.0) < 0.5
    assert _max_octas(0.0) > 0.0  # ...but present on the calm day


# --- Table 2: onset + base, broad bands ------------------------------------- #

def test_table2_onset_early_afternoon() -> None:
    hours, steps = _run(0.0)
    cloudy = np.array([np.isfinite(s.cloud_base_m) for s in steps])
    assert cloudy.any(), "no cumulus formed on the calm day"
    onset_h = hours[np.argmax(cloudy)]
    assert 11.5 <= onset_h <= 15.0  # paper onset ~13:00


def test_table2_cloud_base_band() -> None:
    _, steps = _run(0.0)
    bases = np.array([s.cloud_base_m for s in steps])
    bases = bases[np.isfinite(bases)]
    assert bases.size > 0
    assert np.all((bases >= 1500.0) & (bases <= 2500.0))  # paper 1700 → 2400 m


def test_updraft_speeds_are_glider_scale() -> None:
    _, steps = _run(0.0)
    v_max = max(s.v_max_m_s for s in steps)
    assert 1.0 < v_max < 7.0  # m/s — thermals, not a hurricane
