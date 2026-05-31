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
from alptherm_icon.regions.alpine_v0 import MODEL_BBOX, build_domain_boundary, classify_terrain_type
from alptherm_icon.regions.basins import (
    basins_inside_bbox,
    fetch_hydrobasins,
    union_basins_for_domain,
)
from alptherm_icon.regions.dem import TileId, build_region_dem, tiles_for_geom
from alptherm_icon.regions.edges import compute_edge_thresholds, find_neighbors
from alptherm_icon.regions.polygon import load_region
from alptherm_icon.regions.soiusa import (
    assign_basins_to_groups,
    fetch_osm_mountain_ranges,
    load_groups_from_file,
    realize_groups,
    soiusa_union,
)

__all__ = [
    "AHDProfile",
    "ALPEN_BBOX",
    "MODEL_BBOX",
    "AlpineBasinSummary",
    "TileId",
    "assign_basins_to_groups",
    "basins_inside_bbox",
    "build_domain_boundary",
    "build_region_dem",
    "classify_terrain_type",
    "compute_ahd",
    "compute_edge_thresholds",
    "fetch_hydrobasins",
    "fetch_osm_mountain_ranges",
    "find_neighbors",
    "load_alpine_basins",
    "load_groups_from_file",
    "load_region",
    "realize_groups",
    "soiusa_union",
    "summarise",
    "tiles_for_geom",
    "union_basins_for_domain",
    "write_geojson",
]
