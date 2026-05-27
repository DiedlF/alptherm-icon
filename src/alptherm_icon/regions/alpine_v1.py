"""Quer-Segmentierung langer Alpenrand-Täler (Plan §3.1.1) — Sprint 3.

Das geometrische Gerüst aus :mod:`alpine_v0` plus AHD aus
:mod:`alpine_v0_dem` lässt einen Rest stehen: große, höhen-heterogene
Randbecken, die Hochgebirgsthermik (steile Heizflächen, später Onset,
hohe Basis) mit Flachland-Konvektion (früher Onset, feuchtere Böden)
mischen. Plan §3.1.1 verlangt dort einen zusätzlichen Schnitt nach
Geländecharakter.

Wichtige Vorklärung — *kein* expliziter Hauptkamm-Schnitt nötig:
HydroBASINS-Basins sind Wasserscheiden-Einzugsgebiete; der Alpenhaupt-
kamm ist die kontinentale Hauptwasserscheide (Nordsee/Schwarzes Meer
vs. Mittelmeer/Adria). Kein einzelnes L8-Basin reicht topologisch über
den Hauptkamm. Plan §3.1 Stufe 2 "keine Region über den Hauptkamm" ist
also durch die HydroBASINS-Natur bereits erfüllt. Was bleibt, ist die
Quer-Segmentierung *innerhalb* großer Randbecken.

Verfahren:
1. Kandidaten-Auswahl (siehe ``is_candidate``): groß ODER länglich,
   höhen-heterogen (range > 2000 m) und bis < 800 m runterreichend.
2. Pro Kandidat: DEM auf das Basin maskieren, in drei Höhenbänder
   klassifizieren, jedes Band vektorisieren und mit dem Basin
   verschneiden. Bänder unter ``MIN_SEGMENT_KM2`` werden ins
   benachbarte größere Band gemerged (kein Mikro-Fragment).
3. Nicht-Kandidaten bleiben unverändert (ein Segment = das Basin).

Die Höhenbänder folgen §3.1.1 ("Hochgebirgs-, Voralpen- und
Flachlandsegment"):
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
import rasterio.mask
from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform, unary_union

log = logging.getLogger(__name__)

# Höhenbänder nach §3.1.1. Schwellen in m MSL. Alpine Talsohlen liegen
# höher als das Vorland, daher 800/1500 (nicht die 600-m-Regions-Mittel-
# Schwelle aus alpine_v0_dem).
ELEV_BANDS: tuple[tuple[float, float, str], ...] = (
    (-1e4, 800.0, "flachland"),
    (800.0, 1500.0, "voralpen"),
    (1500.0, 1e5, "hochgebirge"),
)

MIN_SEGMENT_KM2 = 50.0
"""Bänder kleiner als das werden in das benachbarte größere Band gemerged."""

SIMPLIFY_TOLERANCE_M = 150.0
"""Douglas-Peucker-Toleranz für die vektorisierten Bandgrenzen (in der
metrischen Mosaic-CRS). Die Pixel-für-Pixel-Vektorisierung erzeugt sonst
Polygone mit Millionen Vertices (30-m-Treppen); 150 m glättet das auf
brauchbare GeoJSON-Größe ohne thermisch relevanten Informationsverlust."""


@dataclass
class SegmentResult:
    hybas_id: int
    band_label: str
    geometry: BaseGeometry
    area_km2: float


def is_candidate(
    area_km2: float,
    elev_min_m: float,
    elev_range_m: float,
    aspect_ratio: float,
) -> bool:
    """§3.1.1-Kandidat: höhen-heterogen, bis ins Tal reichend, groß/länglich."""
    if not (np.isfinite(elev_range_m) and np.isfinite(elev_min_m)):
        return False
    return (
        elev_range_m > 2000.0
        and elev_min_m < 800.0
        and (aspect_ratio > 2.0 or area_km2 > 1500.0)
    )


def _band_for(elev: np.ndarray) -> np.ndarray:
    """Map an elevation array to band-indices (0/1/2), NaN→ -1."""
    out = np.full(elev.shape, -1, dtype=np.int8)
    for i, (lo, hi, _label) in enumerate(ELEV_BANDS):
        out[(elev >= lo) & (elev < hi)] = i
    return out


def segment_basin_by_bands(
    geom_4326: BaseGeometry,
    hybas_id: int,
    mosaic_src: rasterio.DatasetReader,
    transformer,  # pyproj Transformer 4326 → mosaic CRS
) -> list[SegmentResult]:
    """Split one basin into elevation-band polygons.

    Returns one :class:`SegmentResult` per band that survives the
    ``MIN_SEGMENT_KM2`` filter. Small bands are absorbed into the
    largest band so the basin stays fully covered.
    """
    geom_proj = shp_transform(transformer.transform, geom_4326)
    masked, mask_transform = rasterio.mask.mask(
        mosaic_src, [geom_proj.__geo_interface__], crop=True, filled=False
    )
    elev = masked[0]
    valid_mask = ~np.ma.getmaskarray(elev)
    elev_filled = np.ma.getdata(elev).astype(np.float64)
    band_idx = _band_for(elev_filled)
    band_idx[~valid_mask] = -1  # outside polygon

    pixel_area_km2 = abs(mask_transform.a * mask_transform.e) / 1e6

    # Vectorise each band index into polygons (in mosaic CRS), then
    # reproject back to 4326 for the GeoJSON.
    from pyproj import Transformer

    inv = Transformer.from_crs(mosaic_src.crs, 4326, always_xy=True)
    band_geoms: dict[str, list[BaseGeometry]] = {}
    band_area: dict[str, float] = {}
    for i, (_lo, _hi, label) in enumerate(ELEV_BANDS):
        band_mask = (band_idx == i).astype(np.uint8)
        n_pix = int(band_mask.sum())
        if n_pix == 0:
            continue
        polys = [
            shapely_shape(geo)
            for geo, val in rasterio.features.shapes(
                band_mask, mask=band_mask.astype(bool), transform=mask_transform
            )
            if val == 1
        ]
        if not polys:
            continue
        # Simplify in metric CRS (mosaic) before reprojecting — the
        # pixel-staircase boundary is millions of vertices otherwise.
        # buffer(0) repairs any self-intersections simplify introduces
        # (otherwise a later unary_union throws GEOS TopologyException).
        merged = unary_union(polys).simplify(SIMPLIFY_TOLERANCE_M).buffer(0)
        band_geoms[label] = [shp_transform(inv.transform, merged)]
        band_area[label] = n_pix * pixel_area_km2

    if not band_geoms:
        return []

    # Merge sub-MIN bands into the largest band.
    largest = max(band_area, key=band_area.get)
    keep: dict[str, BaseGeometry] = {}
    small_geoms: list[BaseGeometry] = []
    for label, geoms in band_geoms.items():
        if band_area[label] < MIN_SEGMENT_KM2 and label != largest:
            small_geoms.extend(geoms)
        else:
            keep[label] = unary_union(geoms)
    if small_geoms:
        keep[largest] = unary_union(
            [g.buffer(0) for g in (keep[largest], *small_geoms)]
        )

    return [
        SegmentResult(
            hybas_id=hybas_id,
            band_label=label,
            geometry=geom,
            area_km2=band_area.get(label, 0.0),
        )
        for label, geom in keep.items()
    ]


def build_alpine_v1(
    basins: gpd.GeoDataFrame,
    mosaic_path: Path,
) -> gpd.GeoDataFrame:
    """Apply §3.1.1 cross-segmentation to candidate basins.

    Expects ``basins`` to already carry the columns
    ``area_km2, elev_min_m, elev_range_m, aspect_ratio, habitat_class``
    (produced by the Sprint-1/2 pipeline + risk analysis). Non-candidate
    basins pass through unchanged as a single ``region_id``; candidates
    are split into ``<HYBAS_ID>_<band>`` regions.
    """
    from pyproj import Transformer

    rows: list[dict] = []
    n_split = 0
    with rasterio.open(mosaic_path) as src:
        transformer = Transformer.from_crs(4326, src.crs, always_xy=True)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
            for _, b in basins.iterrows():
                hid = int(b["HYBAS_ID"])
                cand = is_candidate(
                    float(b["area_km2"]),
                    float(b.get("elev_min_m", np.nan)),
                    float(b.get("elev_range_m", np.nan)),
                    float(b.get("aspect_ratio", 1.0)),
                )
                if not cand:
                    rows.append(
                        {
                            "region_id": f"{hid}",
                            "hybas_id": hid,
                            "band": "whole",
                            "area_km2": float(b["area_km2"]),
                            "habitat_class": b.get("habitat_class"),
                            "geometry": b["geometry"],
                        }
                    )
                    continue
                segs = segment_basin_by_bands(
                    b["geometry"], hid, src, transformer
                )
                if len(segs) <= 1:
                    # Couldn't meaningfully split — keep whole.
                    rows.append(
                        {
                            "region_id": f"{hid}",
                            "hybas_id": hid,
                            "band": "whole",
                            "area_km2": float(b["area_km2"]),
                            "habitat_class": b.get("habitat_class"),
                            "geometry": b["geometry"],
                        }
                    )
                    continue
                n_split += 1
                for seg in segs:
                    rows.append(
                        {
                            "region_id": f"{hid}_{seg.band_label}",
                            "hybas_id": hid,
                            "band": seg.band_label,
                            "area_km2": seg.area_km2,
                            "habitat_class": b.get("habitat_class"),
                            "geometry": seg.geometry,
                        }
                    )
    log.info("split %d candidate basins into elevation bands", n_split)
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")

    # Final validity repair: simplify + reprojection can leave a handful
    # of self-touching rings. make_valid() normalises them so downstream
    # consumers (Komp. B aggregation, dashboards) never choke.
    from shapely.validation import make_valid

    invalid = ~out.geometry.is_valid
    n_invalid = int(invalid.sum())
    if n_invalid:
        log.info("repairing %d invalid geometries via make_valid", n_invalid)
        out.loc[invalid, "geometry"] = out.loc[invalid, "geometry"].apply(make_valid)
    return out
