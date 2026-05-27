"""Komponente A — Regionsgeometrie und Area-Height-Distribution (AHD).

Per plan §3:
- §3.1 Regionsdefinition (Stufe 1 hydrologisch, Stufe 2 manuell, Stufe 3 datengetrieben)
- §3.2 AHD-Berechnung (S_G(z) Heizfläche, V_a(z) Restvolumen, 100 m Höhenklassen)
"""

from alptherm_icon.regions.ahd import AHDProfile, compute_ahd
from alptherm_icon.regions.alpine_v0 import (
    ALPEN_BBOX,
    AlpineBasinSummary,
    load_alpine_basins,
    summarise,
    write_geojson,
)
from alptherm_icon.regions.basins import (
    basins_inside_bbox,
    fetch_hydrobasins,
    filter_north_of_inn,
    select_basins,
)
from alptherm_icon.regions.dem import TileId, build_region_dem, tiles_for_geom
from alptherm_icon.regions.polygon import load_region

__all__ = [
    "AHDProfile",
    "ALPEN_BBOX",
    "AlpineBasinSummary",
    "TileId",
    "basins_inside_bbox",
    "build_region_dem",
    "compute_ahd",
    "fetch_hydrobasins",
    "filter_north_of_inn",
    "load_alpine_basins",
    "load_region",
    "select_basins",
    "summarise",
    "tiles_for_geom",
    "write_geojson",
]
