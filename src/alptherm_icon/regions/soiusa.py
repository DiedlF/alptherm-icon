"""SOIUSA/AVE group geometry — Komp. A Stufe 1 + Stufe 2 (plan §3.1).

Stufe 1: load or fetch SOIUSA/AVE mountain-group polygons (the orographic
         *should-be* structure — which regions we want).
Stufe 2: assign HydroBASINS L8/9 basins to groups and realise those groups
         as basin unions with watershed-aligned boundaries.

Sources (in preference order):
  1. Local GeoJSON/Shapefile — authoritative Marazzi/AVE geometry.
  2. OSM Overpass API: ``relation["natural"="mountain_range"]``,
     geometry reconstructed from outer-role member ways.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import geopandas as gpd
import requests
import shapely.geometry
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_BBOX_OVERPASS = "43.5,5.0,49.0,17.0"  # south,west,north,east


# ---------------------------------------------------------------------------
# Stufe 1 — source helpers
# ---------------------------------------------------------------------------

def _reconstruct_polygon(members: list[dict]) -> BaseGeometry | None:
    """Build a Shapely polygon from OSM relation outer-role member ways."""
    outer = []
    for m in members:
        if m.get("role") != "outer" or m.get("type") != "way":
            continue
        coords = [(nd["lon"], nd["lat"]) for nd in m.get("geometry", [])]
        if len(coords) >= 2:
            outer.append(shapely.geometry.LineString(coords))
    if not outer:
        return None
    polys = list(polygonize(linemerge(outer)))
    return unary_union(polys) if polys else None


def fetch_osm_mountain_ranges(
    bbox_overpass: str = _BBOX_OVERPASS,
    overpass_url: str = OVERPASS_URL,
    timeout_s: float = 120.0,
) -> gpd.GeoDataFrame:
    """Query OSM Overpass for natural=mountain_range relations in the bbox.

    Returns a GeoDataFrame (EPSG:4326) with columns:
      osm_id, name, name_de, name_it, soiusa_code, geometry.
    Relations whose outer-way geometry cannot be reconstructed are dropped.
    """
    query = (
        f"[out:json][timeout:{int(timeout_s)}];"
        f'relation["natural"="mountain_range"]({bbox_overpass});'
        "out geom;"
    )
    resp = requests.post(overpass_url, data={"data": query}, timeout=timeout_s + 30)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])
    log.info("OSM returned %d mountain_range relations", len(elements))

    rows = []
    for el in elements:
        tags = el.get("tags", {})
        geom = _reconstruct_polygon(el.get("members", []))
        if geom is None or geom.is_empty:
            log.debug("dropped relation %s (no usable outer ways)", el.get("id"))
            continue
        rows.append({
            "osm_id": int(el["id"]),
            "name": tags.get("name", ""),
            "name_de": tags.get("name:de", tags.get("name", "")),
            "name_it": tags.get("name:it", ""),
            "soiusa_code": tags.get("soiusa", tags.get("ref:soiusa", "")),
            "geometry": geom,
        })
    log.info("reconstructed geometry for %d / %d relations", len(rows), len(elements))
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def load_groups_from_file(path: Path) -> gpd.GeoDataFrame:
    """Load SOIUSA/AVE group geometries from a local GeoJSON or Shapefile.

    Normalises to EPSG:4326. Adds empty-string placeholder columns for
    osm_id, soiusa_code, name, name_de, name_it if absent.
    """
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    for col in ("osm_id", "soiusa_code", "name", "name_de", "name_it"):
        if col not in gdf.columns:
            gdf[col] = ""
    return gdf


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise(groups: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ensure consistent column names for downstream functions."""
    g = groups.copy()
    if "soiusa_name" not in g.columns:
        if "name_de" in g.columns:
            g["soiusa_name"] = g["name_de"].fillna("").astype(str)
        elif "name" in g.columns:
            g["soiusa_name"] = g["name"].fillna("").astype(str)
        else:
            g["soiusa_name"] = g.get("osm_id", g.index.astype(str)).astype(str)
    for col in ("soiusa_code", "osm_id"):
        if col not in g.columns:
            g[col] = ""
    return g


def _safe_region_id(soiusa_name: str, soiusa_code: str) -> str:
    code = soiusa_code.strip()
    if code:
        return f"soiusa_{code}"
    slug = soiusa_name.replace(" ", "_").replace("/", "-")[:40]
    return f"soiusa_{slug}"


# ---------------------------------------------------------------------------
# Stufe 2 — basin assignment and group realisation
# ---------------------------------------------------------------------------

def assign_basins_to_groups(
    basins: gpd.GeoDataFrame,
    groups: gpd.GeoDataFrame,
    method: str = "largest_overlap",
) -> gpd.GeoDataFrame:
    """Assign each HydroBASINS basin to the SOIUSA group it predominantly falls into.

    method='largest_overlap' (default): assign by the group that covers
        the largest intersection area. Robust for basins on group boundaries.
    method='centroid': assign by the group containing the representative
        point. Faster; may misassign basins that straddle two groups.

    Returns ``basins`` with added columns soiusa_name, soiusa_code, osm_id.
    Basins outside all groups get empty-string soiusa_name — they become
    Mittelgebirge/Flachland in the terrain-type classification.
    """
    groups = _normalise(groups)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")

        if method == "largest_overlap":
            b = basins[["HYBAS_ID", "geometry"]].copy()
            g = groups[["soiusa_name", "soiusa_code", "osm_id", "geometry"]].copy()
            ovl = gpd.overlay(b, g, how="intersection", keep_geom_type=False)
            if ovl.empty:
                result = basins.copy()
                for col in ("soiusa_name", "soiusa_code", "osm_id"):
                    result[col] = ""
                return result
            ovl["_area"] = ovl.geometry.area
            idx = ovl.groupby("HYBAS_ID")["_area"].idxmax()
            best = ovl.loc[idx].set_index("HYBAS_ID")
            result = basins.copy()
            for col in ("soiusa_name", "soiusa_code", "osm_id"):
                result[col] = result["HYBAS_ID"].map(best.get(col, {})).fillna("")
            return result

        elif method == "centroid":
            pts = gpd.GeoDataFrame(
                geometry=basins.geometry.representative_point(),
                index=basins.index,
                crs=basins.crs,
            )
            joined = gpd.sjoin(
                pts,
                groups[["soiusa_name", "soiusa_code", "osm_id", "geometry"]],
                how="left",
                predicate="within",
            )
            joined = joined[~joined.index.duplicated(keep="first")]
            result = basins.copy()
            for col in ("soiusa_name", "soiusa_code", "osm_id"):
                result[col] = joined.reindex(basins.index).get(col, "").fillna("").values
            return result

        else:
            raise ValueError(f"unknown method {method!r}; use 'largest_overlap' or 'centroid'")


def realize_groups(
    basins_assigned: gpd.GeoDataFrame,
    groups: gpd.GeoDataFrame | None = None,
) -> gpd.GeoDataFrame:
    """Union assigned basins per SOIUSA group → one realised polygon per group.

    Only basins with a non-empty soiusa_name are processed. If ``groups`` is
    supplied, groups that received no basin are kept using their source polygon
    (fallback so no group is silently lost from the output).

    Returns a GeoDataFrame with columns:
      region_id, soiusa_name, soiusa_code, osm_id,
      n_basins, area_km2, terrain_type, centroid_lat, centroid_lon, geometry.
    """
    groups_norm = _normalise(groups) if groups is not None else None
    alpine = basins_assigned[basins_assigned["soiusa_name"].astype(str) != ""].copy()

    rows = []
    for soiusa_name, grp in alpine.groupby("soiusa_name"):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
            union = grp.geometry.union_all()
            pt = union.representative_point()
        code = str(grp["soiusa_code"].iloc[0]) if "soiusa_code" in grp.columns else ""
        osm_id = str(grp["osm_id"].iloc[0]) if "osm_id" in grp.columns else ""
        area = float(grp["area_km2"].sum()) if "area_km2" in grp.columns else float("nan")
        rows.append({
            "region_id": _safe_region_id(soiusa_name, code),
            "soiusa_name": soiusa_name,
            "soiusa_code": code,
            "osm_id": osm_id,
            "n_basins": len(grp),
            "area_km2": area,
            "terrain_type": "alpine",
            "centroid_lat": pt.y,
            "centroid_lon": pt.x,
            "geometry": union,
        })

    realised_names = {r["soiusa_name"] for r in rows}
    if groups_norm is not None:
        for _, g in groups_norm.iterrows():
            gname = str(g.get("soiusa_name", ""))
            if not gname or gname in realised_names:
                continue
            code = str(g.get("soiusa_code", ""))
            geom = g.geometry
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
                pt = geom.representative_point()
            rows.append({
                "region_id": _safe_region_id(gname, code),
                "soiusa_name": gname,
                "soiusa_code": code,
                "osm_id": str(g.get("osm_id", "")),
                "n_basins": 0,
                "area_km2": float("nan"),
                "terrain_type": "alpine",
                "centroid_lat": pt.y,
                "centroid_lon": pt.x,
                "geometry": geom,
            })
            log.debug("group %r: no basins assigned, using source polygon", gname)

    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def soiusa_union(groups: gpd.GeoDataFrame) -> BaseGeometry:
    """Union of all SOIUSA group polygons — the Alpine inner boundary."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
        return groups.geometry.union_all()
