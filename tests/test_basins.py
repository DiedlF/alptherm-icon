"""Tests for the HydroBASINS helpers in Komp. A.

Pure-geometry tests on synthetic basins — no network, no shapefile loading.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import box

from alptherm_icon.regions.basins import basins_inside_bbox, union_basins_for_domain


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
            (1.0, 1.0, 3.0, 3.0, 1),    # fully inside  -> kept
            (8.0, 8.0, 12.0, 12.0, 2),  # 25% inside   -> dropped
            (-1.0, 4.0, 6.0, 6.0, 3),   # 6/7 inside   -> kept
        ]
    )
    kept = basins_inside_bbox(basins, seed, min_overlap_frac=0.5)
    assert sorted(kept["HYBAS_ID"].tolist()) == [1, 3]


def test_basins_inside_bbox_empty_returns_empty() -> None:
    seed = box(100.0, 100.0, 101.0, 101.0)
    basins = _make_basins([(0.0, 0.0, 1.0, 1.0, 1)])
    kept = basins_inside_bbox(basins, seed)
    assert kept.empty


def test_union_basins_for_domain_covers_all_inside() -> None:
    """Domain boundary is the union of all majority-inside basins."""
    bbox = box(0.0, 0.0, 10.0, 10.0)
    basins = _make_basins(
        [
            (1.0, 1.0, 4.0, 4.0, 1),    # inside
            (5.0, 5.0, 8.0, 8.0, 2),    # inside
            (9.5, 9.5, 12.0, 12.0, 3),  # <50 % inside -> excluded
        ]
    )
    domain = union_basins_for_domain(basins, bbox, min_overlap_frac=0.5)
    # Domain must contain both included basin centroids.
    assert domain.contains(box(1.0, 1.0, 4.0, 4.0).centroid)
    assert domain.contains(box(5.0, 5.0, 8.0, 8.0).centroid)
    # Excluded basin's centroid is outside.
    assert not domain.contains(box(10.5, 10.5, 12.0, 12.0).centroid)
