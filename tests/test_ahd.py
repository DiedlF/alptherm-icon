"""Smoke tests for the AHD computation against a synthetic DEM."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from alptherm_icon.regions.ahd import BIN_HEIGHT_M, compute_ahd


def _write_flat_dem(path: Path, elevation_m: float, width: int = 20, height: int = 20) -> None:
    """Write a flat 1 m × 1 m pixel raster (in EPSG:4326 coords scaled trivially)."""
    transform = from_origin(0.0, height, 1.0, 1.0)
    data = np.full((height, width), elevation_m, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def _write_ramp_dem(path: Path, z_min: float, z_max: float, n: int = 100) -> None:
    """Vertical ramp from z_min (top row) to z_max (bottom row), 1 m × 1 m pixels."""
    transform = from_origin(0.0, n, 1.0, 1.0)
    column = np.linspace(z_min, z_max, n, dtype=np.float32)
    data = np.broadcast_to(column[:, None], (n, n)).copy()
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=n,
        width=n,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def test_ahd_flat_terrain_single_bin(tmp_path: Path) -> None:
    dem = tmp_path / "flat.tif"
    _write_flat_dem(dem, elevation_m=550.0, width=10, height=10)
    # Project the box into EPSG:3857-equivalent coords using a passthrough geometry
    # (the helper reprojects 4326→raster CRS — pick a 4326 box that covers the raster.)
    region = box(-1e-3, -1e-3, 1e-3, 1e-3)

    profile = compute_ahd(dem, region, region_name="flat")
    assert profile.s_g.sum() == pytest.approx(profile.region_area_m2)
    # Single non-empty bin: the one containing 550 m
    nonzero = np.flatnonzero(profile.s_g > 0)
    assert nonzero.size == 1
    bin_idx = nonzero[0]
    assert profile.z_bottom_m[bin_idx] <= 550.0 < profile.z_top_m[bin_idx]


def test_ahd_ramp_terrain_volume_monotonic(tmp_path: Path) -> None:
    dem = tmp_path / "ramp.tif"
    _write_ramp_dem(dem, z_min=1000.0, z_max=2000.0, n=50)
    region = box(-1e-3, -1e-3, 1e-3, 1e-3)

    profile = compute_ahd(dem, region, region_name="ramp")
    # S_G should sum to total region area
    assert profile.s_g.sum() == pytest.approx(profile.region_area_m2)
    # Bin width is the configured constant
    assert profile.z_top_m[0] - profile.z_bottom_m[0] == pytest.approx(BIN_HEIGHT_M)
    # V_a is monotonically non-decreasing with height (terrain only gets out
    # of the way as you go up).
    diffs = np.diff(profile.v_a)
    assert np.all(diffs >= -1e-6)
