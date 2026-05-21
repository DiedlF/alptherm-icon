"""Komponente A — Regionsgeometrie und Area-Height-Distribution (AHD).

Per plan §3:
- §3.1 Regionsdefinition (Stufe 1 hydrologisch, Stufe 2 manuell, Stufe 3 datengetrieben)
- §3.2 AHD-Berechnung (S_G(z) Heizfläche, V_a(z) Restvolumen, 100 m Höhenklassen)
"""

from alptherm_icon.regions.ahd import AHDProfile, compute_ahd
from alptherm_icon.regions.dem import TileId, build_region_dem, tiles_for_geom
from alptherm_icon.regions.polygon import load_region

__all__ = [
    "AHDProfile",
    "TileId",
    "build_region_dem",
    "compute_ahd",
    "load_region",
    "tiles_for_geom",
]
