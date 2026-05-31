"""Region-boundary threshold heights for the coupling graph (plan §3.2 + §5.5).

For each pair of neighbouring regions, finds the lowest DEM elevation in a
thin buffer along the shared polygon boundary — the effective "pass height"
that controls air exchange between adjacent atmospheric columns.

Output: an edge GeoDataFrame, one row per region pair, with columns:
  region_a, region_b, threshold_z_m, valley_floor_a_m, valley_floor_b_m,
  relative_threshold_m, geometry (shared boundary geometry).

The relative_threshold_m drives the permeability factor D in plan §5.5:
  D = max(0, (z_CBL − relative_threshold_m) / z_scale)  clamped to [0, 1].

Computed once alongside the AHD and stored as a static edge GeoJSON.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.mask
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

BOUNDARY_BUFFER_DEG = 0.005  # ~500 m at Alpine latitudes — wide enough to catch a pass


def _min_elev_in_buffer(
    boundary: BaseGeometry,
    mosaic_src: rasterio.DatasetReader,
    transformer,
    buffer_deg: float = BOUNDARY_BUFFER_DEG,
) -> float | None:
    """Return the minimum DEM elevation in a thin buffer around a boundary geometry."""
    from shapely.ops import transform as shp_transform

    buffered = boundary.buffer(buffer_deg)
    if buffered.is_empty:
        return None
    buffered_proj = shp_transform(transformer.transform, buffered)
    try:
        masked, _ = rasterio.mask.mask(
            mosaic_src, [buffered_proj.__geo_interface__], crop=True, filled=False
        )
        data = np.ma.getdata(masked[0]).astype(np.float64)
        valid = ~np.ma.getmaskarray(masked[0])
        elev = data[valid]
        elev = elev[elev > -500.0]
        return float(elev.min()) if elev.size > 0 else None
    except Exception as exc:
        log.debug("mask error: %s", exc)
        return None


def _valley_floor_from_ahd(region_id: str, ahd_dir: Path) -> float | None:
    """Min occupied z_bottom from the region's AHD NetCDF, or None if absent."""
    import xarray as xr

    nc = ahd_dir / f"region_{region_id}_ahd.nc"
    if not nc.exists():
        return None
    try:
        with xr.open_dataset(nc) as ds:
            zb = ds["z_bottom"].values
            sg = ds["s_g"].values
            valid = sg > 0
            if valid.any():
                return float(zb[valid].min())
    except Exception:
        pass
    return None


def find_neighbors(regions: gpd.GeoDataFrame) -> list[tuple[int, int]]:
    """Return positional index pairs (i, j) for regions that share a boundary.

    Uses a small buffer to catch pairs that nominally touch but have
    floating-point gaps after union/simplify operations. Resets the index
    so positional and label indices are identical.
    """
    regions = regions.reset_index(drop=True)
    pairs: set[tuple[int, int]] = set()
    sindex = regions.sindex
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
        for i in range(len(regions)):
            ga = regions.iloc[i].geometry
            for j in sindex.intersection(ga.bounds):
                if j <= i:
                    continue
                gb = regions.iloc[j].geometry
                inter = ga.intersection(gb)
                if not inter.is_empty and inter.geom_type not in ("Point", "MultiPoint"):
                    pairs.add((i, j))
                elif ga.distance(gb) < BOUNDARY_BUFFER_DEG:
                    pairs.add((i, j))
    return sorted(pairs)


def compute_edge_thresholds(
    regions: gpd.GeoDataFrame,
    mosaic_path: Path,
    ahd_dir: Path | None = None,
) -> gpd.GeoDataFrame:
    """Compute DEM pass heights for all neighbouring region pairs.

    Parameters
    ----------
    regions:
        GeoDataFrame of regions; must have a ``region_id`` string column.
    mosaic_path:
        Projected DEM mosaic (output of ``build_alpine_mosaic``).
    ahd_dir:
        Directory containing ``region_<id>_ahd.nc`` files for valley-floor
        lookup. If None, valley floors are estimated from the boundary sample.

    Returns
    -------
    GeoDataFrame with one row per neighbour pair.
    """
    from pyproj import Transformer

    regions = regions.reset_index(drop=True)
    neighbors = find_neighbors(regions)
    log.info("computing threshold heights for %d region-pair edges", len(neighbors))

    rows = []
    with rasterio.open(mosaic_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)

        for idx_a, idx_b in neighbors:
            ra = regions.iloc[idx_a]
            rb = regions.iloc[idx_b]
            rid_a = str(ra["region_id"])
            rid_b = str(rb["region_id"])

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
                shared = ra.geometry.intersection(rb.geometry)

            if shared.is_empty or shared.geom_type in ("Point", "MultiPoint"):
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
                    shared = ra.geometry.boundary.intersection(
                        rb.geometry.buffer(BOUNDARY_BUFFER_DEG)
                    )

            threshold_z = _min_elev_in_buffer(shared, src, transformer)
            if threshold_z is None:
                log.debug("no DEM samples for pair (%s, %s)", rid_a, rid_b)
                continue

            floor_a = (_valley_floor_from_ahd(rid_a, ahd_dir) if ahd_dir else None) or threshold_z
            floor_b = (_valley_floor_from_ahd(rid_b, ahd_dir) if ahd_dir else None) or threshold_z
            rel_threshold = max(0.0, threshold_z - max(floor_a, floor_b))

            rows.append({
                "region_a": rid_a,
                "region_b": rid_b,
                "threshold_z_m": threshold_z,
                "valley_floor_a_m": floor_a,
                "valley_floor_b_m": floor_b,
                "relative_threshold_m": rel_threshold,
                "geometry": shared,
            })

    log.info("computed %d edges", len(rows))
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
