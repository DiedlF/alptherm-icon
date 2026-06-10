"""DEM-derived Alpine perimeter — Komp. A.

A single boundary polygon around the high-elevation Alpine massif, deduced
directly from the DEM instead of an external political boundary. The recipe:

1. downsample the DEM to a working resolution (speed);
2. threshold at ``threshold_m`` → the high-terrain mask;
3. morphological closing to bridge passes, then fill interior holes so deep
   Alpine valleys (Inn, Etsch, …) enclosed by high terrain are kept;
4. keep only the largest connected component — the contiguous Alpine arc, which
   drops the Apennines / Jura / Carpathians / Schwarzwald that also clear the
   threshold but sit across low ground;
5. polygonize and smooth (metric buffer in/out + simplify).

Returns one polygon in EPSG:4326. Tune ``threshold_m`` to trade foreland
inclusion against tightness (≈1000 m gives the classic Alpine outline).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from rasterio.enums import Resampling
from scipy import ndimage
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def _drop_holes(geom: BaseGeometry) -> BaseGeometry:
    """Return the geometry with all interior rings removed (exterior outline only)."""
    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return MultiPolygon([Polygon(p.exterior) for p in geom.geoms])
    return geom


def derive_elevation_perimeter(
    dem_path: str | Path,
    threshold_m: float = 1000.0,
    downsample_factor: int = 8,
    close_iters: int = 3,
    smooth_m: float = 2000.0,
    simplify_m: float = 1500.0,
) -> BaseGeometry:
    """Deduce a single Alpine perimeter polygon (EPSG:4326) from a DEM raster.

    ``downsample_factor`` reads the DEM at 1/factor resolution (a 100 m DEM at
    factor 8 → ~800 m working grid, plenty for a smooth outline). The DEM must be
    in a projected, metric CRS (the smoothing buffers and area are in metres).
    """
    with rasterio.open(dem_path) as ds:
        if ds.crs is None or ds.crs.is_geographic:
            raise ValueError("DEM must be in a projected (metric) CRS for buffering")
        out_h = ds.height // downsample_factor
        out_w = ds.width // downsample_factor
        dem = ds.read(1, out_shape=(out_h, out_w), resampling=Resampling.average).astype("float32")
        transform = ds.transform * ds.transform.scale(ds.width / out_w, ds.height / out_h)
        src_crs = ds.crs

    mask = dem >= threshold_m
    if close_iters > 0:
        mask = ndimage.binary_closing(mask, iterations=close_iters)
    mask = ndimage.binary_fill_holes(mask)

    labels, n = ndimage.label(mask)
    if n == 0:
        raise ValueError(f"no terrain at or above {threshold_m} m in {dem_path}")
    sizes = ndimage.sum(np.ones_like(labels), labels, range(1, n + 1))
    mask = labels == (int(np.argmax(sizes)) + 1)  # largest component = the Alpine arc
    mask = ndimage.binary_fill_holes(mask)

    polys = [
        shape(geom)
        for geom, val in features.shapes(mask.astype("uint8"), mask=mask, transform=transform)
        if val == 1
    ]
    geom = unary_union(polys)
    if smooth_m > 0:
        geom = geom.buffer(smooth_m).buffer(-smooth_m)
    if simplify_m > 0:
        geom = geom.simplify(simplify_m)
    # Fill every interior hole — low basins/valley notches that connect to the
    # foreland through a low corridor survive raster hole-filling, so drop the
    # interior rings to leave one solid outline.
    geom = _drop_holes(geom.buffer(0))

    return gpd.GeoSeries([geom], crs=src_crs).to_crs("EPSG:4326").iloc[0]
