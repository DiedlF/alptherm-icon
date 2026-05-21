"""Tests for the DWD ICON-D2 fetcher (Komp. B).

URL/filename math + the centroid + region-mean extraction logic on a
synthetic xarray dataset. No network calls, no GRIB2 reading — the
actual download is exercised by `python -m alptherm_icon.icon_pipeline
fetch <region> --init <YYYYMMDDHH>` manually.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from alptherm_icon.icon_pipeline.icon import (
    ICON_D2_BASE_URL,
    IconD2File,
    extract_at_region,
)


def test_icon_d2_file_filename_and_url() -> None:
    spec = IconD2File(init=datetime(2026, 5, 21, 0), lead_h=12, var="t_2m")
    assert spec.filename_bz2 == (
        "icon-d2_germany_regular-lat-lon_single-level_2026052100_012_2d_t_2m.grib2.bz2"
    )
    assert spec.filename_grib2 == spec.filename_bz2.removesuffix(".bz2")
    assert spec.url == (
        f"{ICON_D2_BASE_URL}/00/t_2m/{spec.filename_bz2}"
    )


def test_icon_d2_file_lead_zero_and_max() -> None:
    spec0 = IconD2File(init=datetime(2026, 5, 21, 12), lead_h=0, var="asob_s")
    assert "_000_2d_asob_s.grib2.bz2" in spec0.filename_bz2
    spec48 = IconD2File(init=datetime(2026, 5, 21, 12), lead_h=48, var="asob_s")
    assert "_048_2d_asob_s.grib2.bz2" in spec48.filename_bz2


def test_icon_d2_file_rejects_bad_init_hour() -> None:
    with pytest.raises(ValueError, match="init hour"):
        IconD2File(init=datetime(2026, 5, 21, 5), lead_h=0, var="t_2m")


def test_icon_d2_file_rejects_bad_lead() -> None:
    with pytest.raises(ValueError, match="lead_h"):
        IconD2File(init=datetime(2026, 5, 21, 0), lead_h=49, var="t_2m")
    with pytest.raises(ValueError, match="lead_h"):
        IconD2File(init=datetime(2026, 5, 21, 0), lead_h=-1, var="t_2m")


def _make_synthetic_ds(t_2m_field: np.ndarray) -> xr.Dataset:
    """Build a minimal ICON-D2-shaped dataset on a known lat/lon grid."""
    # 0.05° grid, lat-descending (DWD convention)
    lats = np.arange(48.0, 47.0, -0.05)  # 20 lat points, 48.0 -> 47.05
    lons = np.arange(11.0, 12.5, 0.05)  # 30 lon points, 11.0 -> 12.45
    assert t_2m_field.shape == (lats.size, lons.size), (
        f"shape {t_2m_field.shape} != ({lats.size}, {lons.size})"
    )
    return xr.Dataset(
        {"t_2m": (("latitude", "longitude"), t_2m_field)},
        coords={"latitude": lats, "longitude": lons},
    )


def test_extract_at_region_centroid_picks_nearest_grid_point() -> None:
    # Fill with a known gradient so the centroid's nearest-neighbour is unambiguous.
    field = np.zeros((20, 30), dtype=np.float64)
    field[10, 15] = 273.15  # the cell nearest centroid (47.5°N, 11.75°E)
    ds = _make_synthetic_ds(field)
    region = box(11.5, 47.4, 12.0, 47.6)  # centroid = (11.75, 47.5)
    centroid_val, region_mean = extract_at_region(ds, region, "t_2m")
    assert float(centroid_val) == pytest.approx(273.15)
    # Region mean averages many cells, only one of which is non-zero.
    assert 0 < float(region_mean) < 273.15


def test_extract_at_region_mean_matches_uniform_field() -> None:
    field = np.full((20, 30), 290.0, dtype=np.float64)
    ds = _make_synthetic_ds(field)
    region = box(11.5, 47.4, 12.0, 47.6)
    _, region_mean = extract_at_region(ds, region, "t_2m")
    assert float(region_mean) == pytest.approx(290.0)


def test_extract_at_region_mean_two_value_field() -> None:
    """Half of the grid at 300 K, half at 200 K, region covers only the 300 K half."""
    field = np.full((20, 30), 200.0, dtype=np.float64)
    field[:, 15:] = 300.0  # east half = 300 K
    ds = _make_synthetic_ds(field)
    region = box(11.85, 47.4, 12.4, 47.7)  # entirely in the east half
    _, region_mean = extract_at_region(ds, region, "t_2m")
    assert float(region_mean) == pytest.approx(300.0)


def test_extract_at_region_raises_when_no_cells_inside() -> None:
    ds = _make_synthetic_ds(np.zeros((20, 30)))
    region = box(100.0, 0.0, 101.0, 1.0)  # well outside the grid
    with pytest.raises(ValueError, match="no ICON grid cells"):
        extract_at_region(ds, region, "t_2m")
