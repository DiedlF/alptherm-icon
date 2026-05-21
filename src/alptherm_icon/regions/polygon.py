"""Load region polygons from GeoJSON (Stufe 1 output, plan §3.1)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry.base import BaseGeometry


def load_region(path: str | Path, name: str | None = None) -> tuple[BaseGeometry, dict]:
    """Load a single region polygon from a GeoJSON file.

    Parameters
    ----------
    path : str | Path
        Path to a GeoJSON file in EPSG:4326 (plan §3.1: GeoJSON-Polygone in EPSG:4326).
    name : str | None
        If given, filter to the feature with this `name` property. Otherwise
        the file must contain exactly one feature.

    Returns
    -------
    geometry, properties
        Shapely geometry in EPSG:4326 and the feature's property dict.
    """
    gdf = gpd.read_file(path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    if name is not None:
        matches = gdf[gdf.get("name") == name]
        if matches.empty:
            raise ValueError(f"no feature with name={name!r} in {path}")
        row = matches.iloc[0]
    else:
        if len(gdf) != 1:
            raise ValueError(
                f"{path} contains {len(gdf)} features; specify name= to disambiguate"
            )
        row = gdf.iloc[0]

    props = {k: v for k, v in row.items() if k != "geometry"}
    return row.geometry, props
