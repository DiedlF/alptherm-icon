"""Tests for the Copernicus DEM fetch + tile-math helpers.

Network-dependent code is not exercised here — only the URL/filename math
and the bbox→tile mapping. The end-to-end fetch is covered by running
`python -m alptherm_icon.regions fetch-dem <region>` manually.
"""

from __future__ import annotations

from shapely.geometry import box

from alptherm_icon.regions.dem import TileId, tiles_for_bounds, tiles_for_geom


def test_tile_basename_and_url_north_east() -> None:
    tile = TileId(lat=47, lon=11)
    assert tile.basename == "Copernicus_DSM_COG_10_N47_00_E011_00_DEM"
    assert tile.url.endswith(f"/{tile.basename}/{tile.basename}.tif")


def test_tile_basename_south_west() -> None:
    tile = TileId(lat=-3, lon=-65)
    assert tile.basename == "Copernicus_DSM_COG_10_S03_00_W065_00_DEM"


def test_tiles_for_bounds_inntal_pilot() -> None:
    """Inntal/Steinberge bbox spans two 1°x1° tiles: N47 E011 and N47 E012."""
    tiles = tiles_for_bounds(11.20, 47.25, 12.30, 47.75)
    assert tiles == [TileId(47, 11), TileId(47, 12)]


def test_tiles_for_geom_matches_bounds() -> None:
    geom = box(11.20, 47.25, 12.30, 47.75)
    assert tiles_for_geom(geom) == tiles_for_bounds(*geom.bounds)


def test_tiles_for_bounds_single_tile_fully_inside() -> None:
    """A bbox entirely inside one tile still returns that single tile."""
    tiles = tiles_for_bounds(11.4, 47.4, 11.6, 47.6)
    assert tiles == [TileId(47, 11)]


def test_tiles_for_bounds_crossing_integer_boundary() -> None:
    """Crossing a degree boundary picks up the neighbour."""
    tiles = tiles_for_bounds(10.95, 47.4, 11.05, 47.6)
    assert tiles == [TileId(47, 10), TileId(47, 11)]
