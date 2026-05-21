"""DEM acquisition for Komp. A.

Fetches Copernicus GLO-30 tiles from the AWS open-data bucket
(`copernicus-dem-30m`, no auth) and reprojects them into a single
metric-CRS mosaic (EPSG:3035 / ETRS89-LAEA Europe) per region.

`compute_ahd` requires the DEM to be in a projected CRS with metric
pixel sizes — see `ahd.py` — so the reprojection here is load-bearing,
not cosmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.merge import merge as rio_merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry.base import BaseGeometry

COPERNICUS_BUCKET_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
TARGET_CRS = "EPSG:3035"
TARGET_RES_M = 30.0


@dataclass(frozen=True)
class TileId:
    """Integer lat/lon corner of a Copernicus 1° × 1° tile (S/W use negative ints)."""

    lat: int
    lon: int

    @property
    def basename(self) -> str:
        ns = "N" if self.lat >= 0 else "S"
        ew = "E" if self.lon >= 0 else "W"
        return (
            f"Copernicus_DSM_COG_10_{ns}{abs(self.lat):02d}_00_"
            f"{ew}{abs(self.lon):03d}_00_DEM"
        )

    @property
    def url(self) -> str:
        return f"{COPERNICUS_BUCKET_URL}/{self.basename}/{self.basename}.tif"


def tiles_for_bounds(minx: float, miny: float, maxx: float, maxy: float) -> list[TileId]:
    """All Copernicus tiles whose 1°×1° footprint intersects the bbox."""
    lat_lo = math.floor(miny)
    lat_hi = math.ceil(maxy)
    lon_lo = math.floor(minx)
    lon_hi = math.ceil(maxx)
    return [
        TileId(lat=lat, lon=lon)
        for lat in range(lat_lo, lat_hi)
        for lon in range(lon_lo, lon_hi)
    ]


def tiles_for_geom(geom: BaseGeometry) -> list[TileId]:
    minx, miny, maxx, maxy = geom.bounds
    return tiles_for_bounds(minx, miny, maxx, maxy)


def download_tile(tile: TileId, tiles_dir: Path, timeout_s: float = 120.0) -> Path:
    """Download one tile to `tiles_dir/<basename>.tif`. Idempotent."""
    tiles_dir.mkdir(parents=True, exist_ok=True)
    dest = tiles_dir / f"{tile.basename}.tif"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".tif.part")
    with requests.get(tile.url, stream=True, timeout=timeout_s) as resp:
        if resp.status_code == 404:
            raise FileNotFoundError(
                f"Copernicus tile not on AWS bucket: {tile.basename} "
                f"(URL: {tile.url}) — likely an ocean tile with no data."
            )
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def build_region_dem(
    geom: BaseGeometry,
    region_name: str,
    dem_dir: Path,
    tiles_dir: Path | None = None,
    target_crs: str = TARGET_CRS,
    target_res_m: float = TARGET_RES_M,
) -> Path:
    """Fetch the tiles covering `geom`, reproject + mosaic to a single GeoTIFF.

    Output: `dem_dir / f"{region_name}_dem.tif"` in `target_crs`. Idempotent —
    returns the existing file if already built (delete to force rebuild).
    """
    dem_dir.mkdir(parents=True, exist_ok=True)
    out = dem_dir / f"{region_name}_dem.tif"
    if out.exists() and out.stat().st_size > 0:
        return out

    if tiles_dir is None:
        tiles_dir = dem_dir / "tiles"
    tile_paths: list[Path] = []
    for tile in tiles_for_geom(geom):
        try:
            tile_paths.append(download_tile(tile, tiles_dir))
        except FileNotFoundError:
            # Pure-ocean tiles aren't published. Continue with whatever lands.
            continue
    if not tile_paths:
        raise RuntimeError(
            f"no Copernicus tiles found for region {region_name!r} "
            f"(bounds: {geom.bounds}) — region may be fully outside land."
        )

    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        merged, merged_transform = rio_merge(srcs, resampling=Resampling.bilinear)
        src_crs = srcs[0].crs
        src_height, src_width = merged.shape[1], merged.shape[2]
        # Compute warped grid in target CRS at target_res_m resolution.
        west, north = merged_transform * (0, 0)
        east, south = merged_transform * (src_width, src_height)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs,
            target_crs,
            src_width,
            src_height,
            west,
            south,
            east,
            north,
            resolution=target_res_m,
        )
        dst = np.empty((1, dst_height, dst_width), dtype=np.float32)
        reproject(
            source=merged[0],
            destination=dst[0],
            src_transform=merged_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=target_crs,
            resampling=Resampling.bilinear,
        )
    finally:
        for s in srcs:
            s.close()

    tmp = out.with_suffix(".tif.part")
    with rasterio.open(
        tmp,
        "w",
        driver="GTiff",
        height=dst_height,
        width=dst_width,
        count=1,
        dtype="float32",
        crs=target_crs,
        transform=dst_transform,
        compress="deflate",
        tiled=True,
    ) as fh:
        fh.write(dst[0], 1)
    tmp.replace(out)
    return out
