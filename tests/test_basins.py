"""Tests for the HydroBASINS-based region refinement helpers.

Pure-geometry tests on synthetic basins — no network, no shapefile loading.
The end-to-end fetch is exercised via
`python -m alptherm_icon.regions refine-region <name>` manually.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import box

from alptherm_icon.regions.basins import (
    INN_LINE,
    basins_inside_bbox,
    filter_north_of_inn,
    select_basins,
)


def _make_basins(squares: list[tuple[float, float, float, float, int]]) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame of square 'basins' from (minx, miny, maxx, maxy, id) tuples."""
    return gpd.GeoDataFrame(
        {"HYBAS_ID": [s[4] for s in squares]},
        geometry=[box(s[0], s[1], s[2], s[3]) for s in squares],
        crs="EPSG:4326",
    )


def test_basins_inside_bbox_keeps_majority_overlap() -> None:
    seed = box(0.0, 0.0, 10.0, 10.0)
    basins = _make_basins(
        [
            (1.0, 1.0, 3.0, 3.0, 1),   # fully inside  -> kept
            (8.0, 8.0, 12.0, 12.0, 2),  # 25% inside  -> dropped
            (-1.0, 4.0, 6.0, 6.0, 3),  # 6/7 inside  -> kept
        ]
    )
    kept = basins_inside_bbox(basins, seed, min_overlap_frac=0.5)
    assert sorted(kept["HYBAS_ID"].tolist()) == [1, 3]


def test_basins_inside_bbox_empty_returns_empty() -> None:
    seed = box(100.0, 100.0, 101.0, 101.0)
    basins = _make_basins([(0.0, 0.0, 1.0, 1.0, 1)])
    kept = basins_inside_bbox(basins, seed)
    assert kept.empty


def test_filter_north_of_inn_drops_south_basins() -> None:
    a, b = INN_LINE
    # At lon=11.7, Inn lat ≈ 42.59 + 0.41*11.7 ≈ 47.39
    south = (11.6, 47.20, 11.8, 47.30, 100)
    north = (11.6, 47.50, 11.8, 47.60, 101)
    basins = _make_basins([south, north])
    kept = filter_north_of_inn(basins)
    assert kept["HYBAS_ID"].tolist() == [101]


def test_select_basins_pipeline_inntal_synthetic() -> None:
    """Two basins inside Inntal-like bbox: one north of Inn (kept), one south (dropped)."""
    seed = box(11.2, 47.25, 12.3, 47.75)
    north = (11.5, 47.55, 11.9, 47.70, 1)  # centroid at (11.7, 47.625) -> well north
    south = (11.5, 47.30, 11.9, 47.40, 2)  # centroid at (11.7, 47.35)  -> south
    far_out = (15.0, 50.0, 16.0, 51.0, 3)
    basins = _make_basins([north, south, far_out])
    kept = select_basins(basins, seed)
    assert kept["HYBAS_ID"].tolist() == [1]


def test_select_basins_without_hauptkamm_keeps_both_sides() -> None:
    seed = box(11.2, 47.25, 12.3, 47.75)
    north = (11.5, 47.55, 11.9, 47.70, 1)
    south = (11.5, 47.30, 11.9, 47.40, 2)
    basins = _make_basins([north, south])
    kept = select_basins(basins, seed, apply_hauptkamm=False)
    assert sorted(kept["HYBAS_ID"].tolist()) == [1, 2]


def test_inn_line_passes_through_anchor_cities() -> None:
    """Sanity check the calibration: line should be within ~0.05° of each anchor."""
    a, b = INN_LINE
    anchors = {
        "Innsbruck": (11.394, 47.265),
        "Schwaz": (11.708, 47.343),
        "Wörgl": (12.077, 47.485),
        "Kufstein": (12.171, 47.583),
    }
    for name, (lon, lat) in anchors.items():
        residual = lat - (a + b * lon)
        assert abs(residual) < 0.04, f"{name}: line off by {residual:.3f}°"
