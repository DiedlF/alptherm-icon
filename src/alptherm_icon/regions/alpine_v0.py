"""Alpenweites Regions-v0-Gerüst (Plan §3.1 Stufe 1, mit Sonderfall §3.1.1).

Pipeline für die *geometrische* Initial-Definition aller ALPTHERM-Regionen
über den ganzen Alpenraum. Output ist ein GeoJSON mit Klassifizierungs-
Metadaten — die DEM-abhängigen Schritte (mittlere Höhe pro Region, AHD-
Profile, Reliefenergie-basierte Quer-Segmentierung) sind in
``alpine_v0_dem.py`` ausgelagert und können separat (langsamerer
DEM-Batch) gefahren werden, ohne die Pipeline hier zu blockieren.

Was hier passiert:

1. *L8-Clip* auf den Alpen-Bbox (5–17°O, 43.5–49°N). HydroBASINS L8
   ist der Plan-Anker (§3.1 "Level 8 als Anker, Level 9 als
   Verfeinerungsreservoir").

2. *Größen-Filter* zwischen Liechti-Bereich (500–1500 km²) und einer
   gröberen Marge (100–3000 km²). Sehr kleine Basins (Küstenfragmente,
   Inseln) und sehr große (Vorland-Sammelbecken) werden markiert,
   nicht entfernt — die manuelle Stufe-2-Review entscheidet pro Fall.

3. *Klassifizierungs-Attribute*: Centroid-Lat/Lon als Proxy für
   Nord-/Süd-Alpenseite, plus ein ``size_class`` Tag (small / liechti
   / large / huge).

DEM-abhängiges (separat in ``alpine_v0_dem.py``):
- Mittlere Höhe pro Region → alpine vs. Vorland (Plan §3.1.1)
- Reliefenergie → Quer-Segmentierung langer Alpenrand-Täler
- AHD-Profile (Va, SG, Slope, Aspect)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import box

# Plan §9.2: ALPEN_BBOX entspricht der ICON-Sammelmaske. Hier wieder-
# verwendet, damit Regions ⊂ Daten-Abdeckung bleibt.
ALPEN_BBOX = (5.0, 43.5, 17.0, 49.0)

# Plan §3.1: Liechti-Empfehlung 500–1500 km²; wir lassen 100–3000 km²
# als "akzeptabel" durch, mit Marker für Off-Range-Fälle.
SIZE_CLASSES = {
    "small":  (0.0,    100.0),   # Küstenfragmente / Inseln / Detail-Basins
    "small_alpine": (100.0, 500.0),  # Untergrenze, akzeptabel im Hochgebirge
    "liechti": (500.0, 1500.0),   # Plan-konform
    "large":  (1500.0, 3000.0),  # Obergrenze, akzeptabel im Vorland
    "huge":   (3000.0, float("inf")),  # Plan-überschreitend, Split-Kandidat
}


@dataclass
class AlpineBasinSummary:
    n_basins: int
    n_by_size_class: dict[str, int]
    bounds: tuple[float, float, float, float]
    total_area_km2: float


def _classify_size(area_km2: float) -> str:
    for label, (lo, hi) in SIZE_CLASSES.items():
        if lo <= area_km2 < hi:
            return label
    return "huge"


def load_alpine_basins(
    hydrobasins_shp: Path,
    bbox: tuple[float, float, float, float] = ALPEN_BBOX,
    min_overlap_frac: float = 0.5,
) -> gpd.GeoDataFrame:
    """Clip the HydroBASINS L8 EU layer to the Alpine bbox and tag rows.

    Returns a GeoDataFrame with the original HYBAS_ID + PFAF_ID columns
    plus:

    - ``overlap_frac``    — share of basin area inside the bbox
    - ``area_km2``        — from HydroBASINS' SUB_AREA, in km²
    - ``size_class``      — string label per SIZE_CLASSES
    - ``centroid_lat/lon``— representative-point coordinates
    - ``n_side``          — "N" | "C" | "S" relative to a coarse N/S-of-
                              main-ridge approximation (lat = 46.7 — the
                              long-axis of the Alpine main chain at
                              ~46.7°N at the Tirol/Südtirol border)
    """
    gdf = gpd.read_file(hydrobasins_shp)
    bbox_geom = box(*bbox)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
        candidates = gdf[gdf.intersects(bbox_geom)].copy()
        overlap = candidates.geometry.intersection(bbox_geom).area / candidates.geometry.area
        candidates["overlap_frac"] = overlap.values
        clipped = candidates[candidates["overlap_frac"] >= min_overlap_frac].copy()

        clipped["area_km2"] = clipped["SUB_AREA"].astype(float)
        clipped["size_class"] = clipped["area_km2"].apply(_classify_size)
        repr_pts = clipped.geometry.representative_point()
        clipped["centroid_lat"] = repr_pts.y.values
        clipped["centroid_lon"] = repr_pts.x.values

    # Coarse north/central/south band — placeholder until DEM-based
    # Hauptkamm-Korrektur in Stufe 2 die echte Wasserscheide trägt.
    def _band(lat: float) -> str:
        if lat > 47.2:
            return "N"
        if lat < 46.2:
            return "S"
        return "C"

    clipped["n_side"] = clipped["centroid_lat"].apply(_band)
    return clipped.reset_index(drop=True)


def summarise(basins: gpd.GeoDataFrame) -> AlpineBasinSummary:
    counts = basins["size_class"].value_counts().to_dict()
    counts_full = {k: int(counts.get(k, 0)) for k in SIZE_CLASSES}
    bounds = tuple(map(float, basins.total_bounds))
    return AlpineBasinSummary(
        n_basins=len(basins),
        n_by_size_class=counts_full,
        bounds=bounds,  # type: ignore[arg-type]
        total_area_km2=float(basins["area_km2"].sum()),
    )


def write_geojson(basins: gpd.GeoDataFrame, out_path: Path) -> None:
    """Persist as GeoJSON. Drops index, keeps the metadata columns."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # GeoJSON CRS is conventionally EPSG:4326 — our input already is.
    keep_cols = [
        "HYBAS_ID",
        "PFAF_ID",
        "MAIN_BAS",
        "area_km2",
        "size_class",
        "centroid_lat",
        "centroid_lon",
        "n_side",
        "overlap_frac",
        "geometry",
    ]
    cols = [c for c in keep_cols if c in basins.columns]
    basins[cols].to_file(out_path, driver="GeoJSON")
