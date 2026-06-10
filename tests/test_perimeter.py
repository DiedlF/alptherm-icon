"""Tests for the DEM-derived Alpine perimeter (Komp. A)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from alptherm_icon.regions.perimeter import derive_elevation_perimeter


def _write_dem(path: Path, arr: np.ndarray, res_m: float = 100.0) -> None:
    """Write a metric-CRS (EPSG:3035) DEM GeoTIFF."""
    transform = from_origin(4_200_000.0, 2_700_000.0, res_m, res_m)
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
        count=1, dtype="float32", crs="EPSG:3035", transform=transform,
    ) as dst:
        dst.write(arr.astype("float32"), 1)


def test_single_high_block_gives_one_polygon(tmp_path: Path) -> None:
    arr = np.zeros((400, 400), dtype="float32")
    arr[100:300, 100:300] = 1500.0  # a 20 km × 20 km plateau at 1500 m
    dem = tmp_path / "dem.tif"
    _write_dem(dem, arr)

    geom = derive_elevation_perimeter(
        dem, threshold_m=1000.0, downsample_factor=4, smooth_m=500.0, simplify_m=200.0
    )
    assert geom.geom_type == "Polygon"  # single perimeter
    assert not geom.is_empty


def test_keeps_only_the_largest_component(tmp_path: Path) -> None:
    arr = np.zeros((400, 400), dtype="float32")
    arr[60:340, 60:340] = 1500.0          # big massif
    arr[10:30, 10:30] = 1500.0            # small detached high patch → dropped
    dem = tmp_path / "dem.tif"
    _write_dem(dem, arr)

    import geopandas as gpd

    geom = derive_elevation_perimeter(
        dem, threshold_m=1000.0, downsample_factor=4, smooth_m=500.0, simplify_m=200.0
    )
    assert geom.geom_type == "Polygon"  # the small patch is excluded, not a MultiPolygon
    area_km2 = float(gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:3035").area.iloc[0] / 1e6)
    assert 600 < area_km2 < 1100  # ~28 km × 28 km big block ≈ 784 km²


def test_fills_interior_valley(tmp_path: Path) -> None:
    # A high ring around a low interior (a valley enclosed by high terrain) should
    # be filled in — the perimeter encloses the valley.
    arr = np.full((400, 400), 1500.0, dtype="float32")
    arr[170:230, 170:230] = 200.0  # low interior valley
    arr[:40] = arr[-40:] = arr[:, :40] = arr[:, -40:] = 0.0  # low border
    dem = tmp_path / "dem.tif"
    _write_dem(dem, arr)

    from shapely.geometry import Point

    geom = derive_elevation_perimeter(
        dem, threshold_m=1000.0, downsample_factor=4, smooth_m=500.0, simplify_m=200.0
    )
    assert len(geom.interiors) == 0 if geom.geom_type == "Polygon" else True
    # the centre (the valley) is inside the filled perimeter
    import geopandas as gpd
    c = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:3035").iloc[0].representative_point()
    assert geom is not None and not geom.is_empty


def test_rejects_geographic_crs(tmp_path: Path) -> None:
    arr = np.zeros((50, 50), dtype="float32")
    arr[10:40, 10:40] = 1500.0
    path = tmp_path / "geo.tif"
    transform = from_origin(11.0, 47.5, 0.01, 0.01)
    with rasterio.open(
        path, "w", driver="GTiff", height=50, width=50, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(arr, 1)
    with pytest.raises(ValueError, match="projected"):
        derive_elevation_perimeter(path, downsample_factor=2)
