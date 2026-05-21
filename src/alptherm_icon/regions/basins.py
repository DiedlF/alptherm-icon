"""HydroBASINS-driven region refinement for Komp. A (plan §3.1 Stufe 2).

Fetches the HydroBASINS (HydroSHEDS) standard product from the public CDN,
selects basins majority-inside a seed bbox, and applies a manual
"hauptkamm" split — for the Inntal pilot, keep only basins draining
from the north side of the Inn river. The Inn line is encoded as a
linear approximation between Innsbruck and Kufstein.
"""

from __future__ import annotations

import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry.base import BaseGeometry

HYDROBASINS_URLS: dict[tuple[str, int], str] = {
    ("eu", 8): "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_eu_lev08_v1c.zip",
    ("eu", 9): "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_eu_lev09_v1c.zip",
    ("eu", 10): "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_eu_lev10_v1c.zip",
}

INN_LINE = (42.8134, 0.3891)
"""Linear approximation of the Lower Inn river: lat = a + b * lon.

Least-squares fit through Innsbruck (11.394, 47.265), Schwaz
(11.708, 47.343), Wörgl (12.077, 47.485), Kufstein (12.171, 47.583).
Max residual ~0.035° latitude (~4 km) — well below Level 8 basin size
(~25 km diameter), so adequate for ridge-side classification.
"""


def fetch_hydrobasins(
    data_dir: Path,
    region: str = "eu",
    level: int = 8,
    timeout_s: float = 600.0,
) -> Path:
    """Download + extract a HydroBASINS shapefile. Idempotent."""
    key = (region, level)
    if key not in HYDROBASINS_URLS:
        raise ValueError(
            f"no HydroBASINS URL configured for region={region!r} level={level}"
        )
    url = HYDROBASINS_URLS[key]
    data_dir.mkdir(parents=True, exist_ok=True)
    shp_name = f"hybas_{region}_lev{level:02d}_v1c.shp"
    shp_path = data_dir / shp_name
    if shp_path.exists():
        return shp_path

    zip_path = data_dir / f"hybas_{region}_lev{level:02d}_v1c.zip"
    tmp = zip_path.with_suffix(".zip.part")
    with requests.get(url, stream=True, timeout=timeout_s) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(data_dir)
    return shp_path


def basins_inside_bbox(
    basins: gpd.GeoDataFrame,
    bbox_geom: BaseGeometry,
    min_overlap_frac: float = 0.5,
) -> gpd.GeoDataFrame:
    """Basins with at least `min_overlap_frac` of their area inside `bbox_geom`.

    Both inputs must be in the same CRS. Fraction is dimensionless so an
    EPSG:4326 input is fine for small regions where degree distortion is
    consistent across the candidate basins.
    """
    candidates = basins[basins.intersects(bbox_geom)].copy()
    if candidates.empty:
        return candidates
    with warnings.catch_warnings():
        # Area is degree² when CRS is geographic — fine here, we only use the ratio.
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
        overlap = candidates.geometry.intersection(bbox_geom).area
        candidates["overlap_frac"] = overlap / candidates.geometry.area
    return candidates[candidates["overlap_frac"] >= min_overlap_frac].copy()


def filter_north_of_inn(
    basins: gpd.GeoDataFrame,
    line_params: tuple[float, float] = INN_LINE,
) -> gpd.GeoDataFrame:
    """Keep basins whose centroid lies north of `lat = a + b * lon`."""
    a, b = line_params
    # representative_point() avoids the geographic-CRS centroid warning and
    # is guaranteed to lie inside the basin — for north/south-of-line
    # classification either an interior point or the centroid works.
    points = basins.geometry.representative_point()
    return basins[points.y > a + b * points.x].copy()


def select_basins(
    basins: gpd.GeoDataFrame,
    seed_geom: BaseGeometry,
    min_overlap_frac: float = 0.5,
    apply_hauptkamm: bool = True,
    line_params: tuple[float, float] = INN_LINE,
) -> gpd.GeoDataFrame:
    """End-to-end basin selection: bbox overlap + optional hauptkamm filter."""
    inside = basins_inside_bbox(basins, seed_geom, min_overlap_frac=min_overlap_frac)
    if not apply_hauptkamm:
        return inside
    return filter_north_of_inn(inside, line_params=line_params)
