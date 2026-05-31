"""CLI for Komp. A — region geometry and AHD.

    python -m alptherm_icon.regions fetch-dem <region>
    python -m alptherm_icon.regions build     <region>

`build` is the full pipeline: load polygon → ensure DEM → compute AHD →
write NetCDF. `fetch-dem` runs only the DEM step (useful for warming the
tile cache before going offline).

Region name = basename of `configs/regions/<name>.geojson`.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import json

import geopandas as gpd
import shapely.geometry

from alptherm_icon.regions.ahd import compute_ahd
from alptherm_icon.regions.alpine_v0 import (
    ALPEN_BBOX,
    MODEL_BBOX,
    build_domain_boundary,
    classify_terrain_type,
    load_alpine_basins,
    summarise,
    write_geojson,
)
from alptherm_icon.regions.alpine_v0_dem import (
    annotate_basins,
    annotate_regions,
    build_alpine_mosaic,
    compute_ahd_batch,
    download_alpine_tiles,
)
from alptherm_icon.regions.alpine_v1 import build_alpine_v1
from alptherm_icon.regions.basins import fetch_hydrobasins, union_basins_for_domain
from alptherm_icon.regions.dem import build_region_dem
from alptherm_icon.regions.edges import compute_edge_thresholds
from alptherm_icon.regions.polygon import load_region
from alptherm_icon.regions.soiusa import (
    assign_basins_to_groups,
    fetch_osm_mountain_ranges,
    load_groups_from_file,
    realize_groups,
    soiusa_union,
)


def _project_root() -> Path:
    """Walk up from CWD looking for pyproject.toml."""
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(
        f"could not locate project root (no pyproject.toml at or above {here})"
    )


def _config_path(root: Path, region: str) -> Path:
    path = root / "configs" / "regions" / f"{region}.geojson"
    if not path.exists():
        raise FileNotFoundError(f"no region config at {path}")
    return path


def cmd_fetch_dem(args: argparse.Namespace) -> int:
    root = _project_root()
    geom, props = load_region(_config_path(root, args.region), name=args.region)
    print(f"region={args.region!r} bounds={geom.bounds} status={props.get('status')}")
    out = build_region_dem(geom, args.region, dem_dir=root / "data" / "dem")
    print(f"DEM mosaic: {out.relative_to(root)} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


def cmd_refine_region(args: argparse.Namespace) -> int:
    root = _project_root()
    config_path = _config_path(root, args.region)
    seed_geom, props = load_region(config_path, name=args.region)
    # Prefer the original seed_bounds from props (set on first refinement) so
    # re-running refine-region is idempotent — otherwise each run would use
    # the previously-refined geometry as the new seed and the region would grow.
    if "seed_bounds" in props:
        seed_bounds = tuple(props["seed_bounds"])
        seed_bbox = shapely.geometry.box(*seed_bounds)
    else:
        seed_bounds = seed_geom.bounds
        seed_bbox = shapely.geometry.box(*seed_bounds)
    print(
        f"region={args.region!r} seed bounds={seed_bounds} "
        f"status={props.get('status')}"
    )

    basins_dir = root / "data" / "basins"
    shp = fetch_hydrobasins(basins_dir, region=args.basins_region, level=args.level)
    print(f"HydroBASINS: {shp.relative_to(root)}")
    basins = gpd.read_file(shp)
    print(f"  loaded {len(basins)} basins (region={args.basins_region!r} lev={args.level})")

    from alptherm_icon.regions.basins import basins_inside_bbox
    selected = basins_inside_bbox(basins, seed_bbox, min_overlap_frac=args.min_overlap)
    print(f"  selected {len(selected)} basins (min_overlap={args.min_overlap})")
    if selected.empty:
        raise RuntimeError("no basins survived selection — try lowering --min-overlap")

    # Clip each basin to the seed bbox so the polygon respects watershed
    # boundaries inside the bbox but doesn't bleed into adjoining basins
    # outside it (Level 8 basins are ~700 km², larger than typical bbox edges).
    if args.no_clip:
        union = selected.geometry.union_all()
    else:
        union = selected.geometry.intersection(seed_bbox).union_all()
    refined_props = {
        **{k: v for k, v in props.items() if k not in {"status", "note"}},
        "status": "refined",
        "source": f"hydrobasins-{args.basins_region}-lev{args.level:02d}",
        "n_basins": int(len(selected)),
        "basin_ids": [int(x) for x in selected["HYBAS_ID"].tolist()],
        "clipped_to_seed_bbox": not args.no_clip,
        "seed_bounds": list(seed_bounds),
    }
    feature = {
        "type": "Feature",
        "properties": refined_props,
        "geometry": shapely.geometry.mapping(union),
    }
    fc = {
        "type": "FeatureCollection",
        "name": props.get("name", args.region),
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": [feature],
    }
    config_path.write_text(json.dumps(fc, indent=2) + "\n")
    print(
        f"refined polygon: {config_path.relative_to(root)} "
        f"area~{union.area:.4f} deg² ({len(selected)} basins)"
    )
    return 0


def cmd_alpine_v0(args: argparse.Namespace) -> int:
    """Sprint 1 from plan §3.1 Stufe 1 — clip HydroBASINS L8 to the
    Alpine bbox and write a tagged GeoJSON gerüst. No DEM, no manual
    Hauptkamm; just the geometric initial layer that Komp. B M2 can
    aggregate over.
    """
    root = _project_root()
    basins_dir = root / "data" / "basins"
    shp = fetch_hydrobasins(basins_dir, region="eu", level=8)
    print(f"HydroBASINS L8: {shp.relative_to(root)}")
    basins = load_alpine_basins(shp, bbox=ALPEN_BBOX, min_overlap_frac=args.min_overlap)
    s = summarise(basins)
    print(f"selected {s.n_basins:,} basins ({s.total_area_km2:,.0f} km² total)")
    for k, v in s.n_by_size_class.items():
        print(f"  size_class={k:14s} n={v:4d}")

    out = root / "data" / "regions" / "alpine_v0_basins.geojson"
    write_geojson(basins, out)
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


def cmd_alpine_v0_dem(args: argparse.Namespace) -> int:
    """Sprint 2a: download all Copernicus tiles for the Alpine bbox +
    build a single EPSG:3035 mosaic."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    tiles_dir = root / "data" / "dem" / "tiles"

    if not args.skip_download:
        s = download_alpine_tiles(tiles_dir)
        print(
            f"tiles: {s.downloaded} downloaded + {s.cached} cached "
            f"+ {s.missing} missing/ocean, {s.bytes_total / 1e9:.1f} GB total"
        )

    mosaic = root / "data" / "dem" / "alpine_v0_dem.tif"
    if not args.skip_mosaic:
        build_alpine_mosaic(tiles_dir, mosaic)
        print(f"mosaic: {mosaic.relative_to(root)} ({mosaic.stat().st_size / 1e9:.2f} GB)")
    return 0


def cmd_alpine_v0_ahd(args: argparse.Namespace) -> int:
    """Sprint 2b: per-region AHD against the shared mosaic + write
    annotated GeoJSON.
    """
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    geojson_in = root / "data" / "regions" / "alpine_v0_basins.geojson"
    mosaic = root / "data" / "dem" / "alpine_v0_dem.tif"
    ahd_dir = root / "data" / "regions" / "alpine_v0_ahd"
    geojson_out = root / "data" / "regions" / "alpine_v0_basins_annotated.geojson"

    if not geojson_in.exists():
        raise FileNotFoundError(
            f"missing {geojson_in.relative_to(root)} — run `alpine-v0` first"
        )
    if not mosaic.exists():
        raise FileNotFoundError(
            f"missing {mosaic.relative_to(root)} — run `alpine-v0-dem` first"
        )

    basins = gpd.read_file(geojson_in)
    print(f"computing AHD for {len(basins)} regions…")
    results = compute_ahd_batch(basins, mosaic, ahd_dir, overwrite=args.force)
    print(f"AHD profiles written: {len(results)} → {ahd_dir.relative_to(root)}")

    annotated = annotate_basins(basins, results)
    annotated.to_file(geojson_out, driver="GeoJSON")
    n_alpine = (annotated["habitat_class"] == "alpine").sum()
    n_vorland = (annotated["habitat_class"] == "vorland").sum()
    print(
        f"annotated GeoJSON: {geojson_out.relative_to(root)} "
        f"(alpine={n_alpine}, vorland={n_vorland})"
    )
    return 0


def _enrich_with_risk_columns(basins, ahd_dir: Path):
    """Add elev_min_m / elev_range_m / aspect_ratio columns from AHD NetCDFs."""
    import numpy as np
    import xarray as xr

    mn, rng = [], []
    for hid in basins["HYBAS_ID"]:
        nc = ahd_dir / f"region_{int(hid)}_ahd.nc"
        if not nc.exists():
            mn.append(float("nan"))
            rng.append(float("nan"))
            continue
        with xr.open_dataset(nc) as ds:
            zb, zt, sg = ds["z_bottom"].values, ds["z_top"].values, ds["s_g"].values
            v = sg > 0
            if not v.any():
                mn.append(float("nan"))
                rng.append(float("nan"))
                continue
            mn.append(float(zb[v].min()))
            rng.append(float(zt[v].max() - zb[v].min()))
    basins = basins.copy()
    basins["elev_min_m"] = mn
    basins["elev_range_m"] = rng

    def _aspect(geom):
        minx, miny, maxx, maxy = geom.bounds
        dx = (maxx - minx) * 111.0 * np.cos(np.radians((miny + maxy) / 2))
        dy = (maxy - miny) * 111.0
        lo, hi = min(dx, dy), max(dx, dy)
        return hi / lo if lo > 0 else float("inf")

    basins["aspect_ratio"] = basins.geometry.apply(_aspect)
    return basins


def cmd_alpine_v1(args: argparse.Namespace) -> int:
    """Sprint 3: §3.1.1 Quer-Segmentierung der höhen-heterogenen
    Randbecken nach Höhenbändern."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    geojson_in = root / "data" / "regions" / "alpine_v0_basins_annotated.geojson"
    ahd_dir = root / "data" / "regions" / "alpine_v0_ahd"
    mosaic = root / "data" / "dem" / "alpine_v0_dem.tif"
    geojson_out = root / "data" / "regions" / "alpine_v1_basins.geojson"

    for p in (geojson_in, mosaic):
        if not p.exists():
            raise FileNotFoundError(f"missing {p.relative_to(root)} — run Sprint 1/2 first")

    basins = gpd.read_file(geojson_in)
    basins = _enrich_with_risk_columns(basins, ahd_dir)
    print(f"loaded {len(basins)} basins, enriched with elevation/aspect")

    v1 = build_alpine_v1(basins, mosaic)
    v1.to_file(geojson_out, driver="GeoJSON")

    n_whole = int((v1["band"] == "whole").sum())
    n_seg = len(v1) - n_whole
    n_parents = v1[v1["band"] != "whole"]["hybas_id"].nunique()
    print(
        f"alpine-v1: {len(v1)} regions "
        f"({n_whole} unverändert, {n_seg} Segmente aus {n_parents} gesplitteten Becken)"
    )
    print(f"  band distribution: {v1['band'].value_counts().to_dict()}")
    print(f"wrote {geojson_out.relative_to(root)} ({geojson_out.stat().st_size / 1e6:.1f} MB)")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    root = _project_root()
    geom, props = load_region(_config_path(root, args.region), name=args.region)
    print(f"region={args.region!r} bounds={geom.bounds} status={props.get('status')}")

    dem_path = build_region_dem(geom, args.region, dem_dir=root / "data" / "dem")
    print(f"DEM mosaic: {dem_path.relative_to(root)}")

    profile = compute_ahd(dem_path, geom, region_name=args.region)
    ds = profile.to_dataset()
    out_dir = root / "data" / "regions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.region}_ahd.nc"
    ds.to_netcdf(out)
    print(
        f"AHD: {out.relative_to(root)} "
        f"area={profile.region_area_m2 / 1e6:.1f} km² "
        f"bins={profile.s_g.size} "
        f"z_range=[{profile.z_bottom_m[0]:.0f}, {profile.z_top_m[-1]:.0f}] m"
    )
    return 0


# ---------------------------------------------------------------------------
# v2 pipeline — SOIUSA-based region definition (plan §3.1 new approach)
# ---------------------------------------------------------------------------

def cmd_domain_boundary(args: argparse.Namespace) -> int:
    """Build the outer model domain from a HydroBASINS union (plan §3.1)."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    basins_dir = root / "data" / "basins"
    shp = fetch_hydrobasins(basins_dir, region="eu", level=args.level)
    print(f"HydroBASINS L{args.level}: {shp.relative_to(root)}")
    basins = gpd.read_file(shp)
    bbox_geom = shapely.geometry.box(*MODEL_BBOX)
    from alptherm_icon.regions.basins import basins_inside_bbox, union_basins_for_domain
    domain = union_basins_for_domain(basins, bbox_geom, min_overlap_frac=args.min_overlap)
    out = root / "data" / "regions" / "domain_boundary.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    import json
    out.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "source": f"hydrobasins-eu-lev{args.level:02d}-union",
                "bbox": list(MODEL_BBOX),
            },
            "geometry": shapely.geometry.mapping(domain),
        }]
    }, indent=2) + "\n")
    print(f"wrote {out.relative_to(root)}")
    return 0


def cmd_soiusa_groups(args: argparse.Namespace) -> int:
    """Fetch or load SOIUSA/AVE group geometries (plan §3.1 Stufe 1)."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    out = root / "data" / "regions" / "soiusa_groups.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.from_file:
        groups = load_groups_from_file(Path(args.from_file))
        print(f"loaded {len(groups)} groups from {args.from_file}")
    else:
        print("querying OSM Overpass for natural=mountain_range …")
        groups = fetch_osm_mountain_ranges(timeout_s=args.timeout)
        print(f"fetched {len(groups)} groups with usable geometry")

    groups.to_file(out, driver="GeoJSON")
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e3:.0f} kB, {len(groups)} features)")
    return 0


def _build_non_alpine_regions(basins: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Convert unassigned HydroBASINS into individual non-alpine regions."""
    rows = []
    for _, row in basins.iterrows():
        hid = int(row["HYBAS_ID"])
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
            pt = row.geometry.representative_point()
        rows.append({
            "region_id": f"hb8_{hid}",
            "soiusa_name": "",
            "soiusa_code": "",
            "osm_id": "",
            "n_basins": 1,
            "area_km2": float(row.get("area_km2", float("nan"))),
            "terrain_type": "non_alpine",
            "centroid_lat": pt.y,
            "centroid_lon": pt.x,
            "geometry": row["geometry"],
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def cmd_alpine_v2(args: argparse.Namespace) -> int:
    """SOIUSA-based region builder — Stufe 1 + 2 (plan §3.1 new approach)."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    basins_dir = root / "data" / "basins"

    shp = fetch_hydrobasins(basins_dir, region="eu", level=args.level)
    print(f"HydroBASINS L{args.level}: {shp.relative_to(root)}")
    basins = load_alpine_basins(shp, bbox=MODEL_BBOX, min_overlap_frac=0.5)
    print(f"  {len(basins)} basins in model domain")

    groups_path = Path(args.groups) if args.groups else root / "data" / "regions" / "soiusa_groups.geojson"

    if groups_path.exists():
        groups = load_groups_from_file(groups_path)
        print(f"  {len(groups)} SOIUSA groups from {groups_path.name}")
        basins = assign_basins_to_groups(basins, groups, method=args.method)
        n_assigned = int((basins["soiusa_name"].astype(str) != "").sum())
        print(f"  {n_assigned} / {len(basins)} basins assigned to SOIUSA groups")
        alpine_regions = realize_groups(basins, groups)
        print(f"  {len(alpine_regions)} Alpine regions realised")
        non_alpine_basins = basins[basins["soiusa_name"].astype(str) == ""].copy()
        su = soiusa_union(groups)
    else:
        print("  no soiusa_groups.geojson — all basins treated as non-alpine (run soiusa-groups first)")
        alpine_regions = gpd.GeoDataFrame(columns=["region_id", "soiusa_name", "soiusa_code",
                                                     "osm_id", "n_basins", "area_km2",
                                                     "terrain_type", "centroid_lat",
                                                     "centroid_lon", "geometry"],
                                           geometry="geometry", crs="EPSG:4326")
        non_alpine_basins = basins.copy()
        for col in ("soiusa_name", "soiusa_code", "osm_id"):
            non_alpine_basins[col] = ""
        import shapely.geometry as _sg
        su = _sg.GeometryCollection()

    non_alpine_regions = _build_non_alpine_regions(non_alpine_basins)
    non_alpine_regions = classify_terrain_type(non_alpine_regions, su)
    print(f"  {len(non_alpine_regions)} non-alpine regions (terrain_type to be refined after DEM step)")

    import pandas as pd
    all_regions = gpd.GeoDataFrame(
        pd.concat([alpine_regions, non_alpine_regions], ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )
    print(f"  total regions: {len(all_regions)}")

    out = root / "data" / "regions" / "alpine_v2_regions.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    all_regions.to_file(out, driver="GeoJSON")
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e6:.1f} MB)")
    tt = all_regions["terrain_type"].value_counts().to_dict()
    for k, v in sorted(tt.items()):
        print(f"  terrain_type={k}: {v}")
    return 0


def cmd_alpine_v2_dem(args: argparse.Namespace) -> int:
    """Download Copernicus tiles + build mosaic for the v2 model domain."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    tiles_dir = root / "data" / "dem" / "tiles"

    if not args.skip_download:
        s = download_alpine_tiles(tiles_dir, bbox=MODEL_BBOX)
        print(
            f"tiles: {s.downloaded} downloaded + {s.cached} cached "
            f"+ {s.missing} missing/ocean, {s.bytes_total / 1e9:.1f} GB total"
        )

    mosaic = root / "data" / "dem" / "alpine_v2_dem.tif"
    if not args.skip_mosaic:
        build_alpine_mosaic(tiles_dir, mosaic, bbox=MODEL_BBOX)
        print(f"mosaic: {mosaic.relative_to(root)} ({mosaic.stat().st_size / 1e9:.2f} GB)")
    return 0


def _enrich_v2_with_risk_columns(regions: gpd.GeoDataFrame, ahd_dir: Path) -> gpd.GeoDataFrame:
    """Add elev_min_m / elev_range_m / aspect_ratio from v2 AHD NetCDFs."""
    import numpy as np
    import xarray as xr

    mn, rng = [], []
    for rid in regions["region_id"]:
        nc = ahd_dir / f"region_{rid}_ahd.nc"
        if not nc.exists():
            mn.append(float("nan"))
            rng.append(float("nan"))
            continue
        with xr.open_dataset(nc) as ds:
            zb, zt, sg = ds["z_bottom"].values, ds["z_top"].values, ds["s_g"].values
            v = sg > 0
            if not v.any():
                mn.append(float("nan"))
                rng.append(float("nan"))
                continue
            mn.append(float(zb[v].min()))
            rng.append(float(zt[v].max() - zb[v].min()))

    regions = regions.copy()
    regions["elev_min_m"] = mn
    regions["elev_range_m"] = rng

    def _aspect(geom: "shapely.geometry.base.BaseGeometry") -> float:
        minx, miny, maxx, maxy = geom.bounds
        dx = (maxx - minx) * 111.0 * np.cos(np.radians((miny + maxy) / 2))
        dy = (maxy - miny) * 111.0
        lo, hi = min(dx, dy), max(dx, dy)
        return hi / lo if lo > 0 else float("inf")

    regions["aspect_ratio"] = regions.geometry.apply(_aspect)
    return regions


def cmd_alpine_v2_ahd(args: argparse.Namespace) -> int:
    """Compute per-region AHD for v2 regions + optionally compute edges."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    geojson_in = root / "data" / "regions" / "alpine_v2_regions.geojson"
    ahd_dir = root / "data" / "regions" / "alpine_v2_ahd"
    geojson_out = root / "data" / "regions" / "alpine_v2_regions_annotated.geojson"

    # Accept an explicit mosaic path, or auto-detect v2 → v0 fallback.
    if args.mosaic_path:
        mosaic = Path(args.mosaic_path)
    else:
        mosaic = root / "data" / "dem" / "alpine_v2_dem.tif"
        if not mosaic.exists():
            fallback = root / "data" / "dem" / "alpine_v0_dem.tif"
            if fallback.exists():
                print(f"alpine_v2_dem.tif not found — using existing {fallback.name}")
                mosaic = fallback
    if not geojson_in.exists():
        raise FileNotFoundError(
            f"missing {geojson_in.relative_to(root)} — run alpine-v2 first"
        )
    if not mosaic.exists():
        raise FileNotFoundError(
            f"no DEM mosaic found — run alpine-v2-dem or pass --mosaic-path"
        )

    regions = gpd.read_file(geojson_in)
    print(f"computing AHD for {len(regions)} v2 regions…")
    results = compute_ahd_batch(
        regions, mosaic, ahd_dir, overwrite=args.force, region_id_col="region_id"
    )
    print(f"AHD profiles: {len(results)} → {ahd_dir.relative_to(root)}")

    annotated = annotate_regions(regions, results)
    annotated.to_file(geojson_out, driver="GeoJSON")
    tt = annotated["terrain_type"].value_counts().to_dict()
    print(f"annotated GeoJSON: {geojson_out.relative_to(root)}")
    for k, v in sorted(tt.items()):
        print(f"  terrain_type={k}: {v}")

    if args.edges:
        print("computing edge threshold heights…")
        edges = compute_edge_thresholds(annotated, mosaic, ahd_dir=ahd_dir)
        edges_out = root / "data" / "regions" / "alpine_v2_edges.geojson"
        edges.to_file(edges_out, driver="GeoJSON")
        print(f"edges: {edges_out.relative_to(root)} ({len(edges)} pairs)")
    return 0


def cmd_alpine_v3(args: argparse.Namespace) -> int:
    """Optional §3.1.1 Quer-Segmentierung applied to v2 regions."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    root = _project_root()
    geojson_in = root / "data" / "regions" / "alpine_v2_regions_annotated.geojson"
    ahd_dir = root / "data" / "regions" / "alpine_v2_ahd"
    mosaic = root / "data" / "dem" / "alpine_v2_dem.tif"
    geojson_out = root / "data" / "regions" / "alpine_v3_regions.geojson"

    for p in (geojson_in, mosaic):
        if not p.exists():
            raise FileNotFoundError(
                f"missing {p.relative_to(root)} — run alpine-v2-ahd first"
            )

    regions = gpd.read_file(geojson_in)
    regions = _enrich_v2_with_risk_columns(regions, ahd_dir)
    print(f"loaded {len(regions)} v2 regions, enriched with elevation/aspect")

    # build_alpine_v1 expects HYBAS_ID; for v2 we create a shim column.
    shim = regions.copy()
    shim["HYBAS_ID"] = range(len(shim))
    shim["area_km2"] = shim.get("area_km2", 0.0)
    shim["habitat_class"] = shim.get("terrain_type", "non_alpine")
    v3 = build_alpine_v1(shim, mosaic)

    # Restore region_id from the original data via hybas_id shim index.
    id_map = {i: str(rid) for i, rid in enumerate(regions["region_id"])}
    v3["region_id"] = v3["hybas_id"].map(id_map).fillna("").apply(
        lambda base: base if v3.loc[v3["hybas_id"].map(id_map) == base, "band"].iloc[0] == "whole"
        else f"{base}_{v3.loc[v3['hybas_id'].map(id_map) == base, 'band'].iloc[0]}"
        if len(v3[v3["hybas_id"].map(id_map) == base]) else base
    ) if False else v3.apply(
        lambda row: id_map.get(row["hybas_id"], str(row["hybas_id"]))
        + ("" if row["band"] == "whole" else f"_{row['band']}"),
        axis=1,
    )

    v3.to_file(geojson_out, driver="GeoJSON")
    n_whole = int((v3["band"] == "whole").sum())
    n_seg = len(v3) - n_whole
    n_parents = v3[v3["band"] != "whole"]["hybas_id"].nunique()
    print(
        f"alpine-v3: {len(v3)} regions "
        f"({n_whole} unverändert, {n_seg} Segmente aus {n_parents} gesplitteten)"
    )
    print(f"wrote {geojson_out.relative_to(root)} ({geojson_out.stat().st_size / 1e6:.1f} MB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.regions")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch-dem", help="download Copernicus tiles + build mosaic")
    p_fetch.add_argument("region", help="region name (configs/regions/<name>.geojson)")
    p_fetch.set_defaults(func=cmd_fetch_dem)

    p_refine = sub.add_parser(
        "refine-region",
        help="refine a placeholder polygon using HydroBASINS catchments",
    )
    p_refine.add_argument("region")
    p_refine.add_argument("--level", type=int, default=8, help="HydroBASINS Pfafstetter level (default: 8)")
    p_refine.add_argument("--basins-region", default="eu", help="HydroBASINS region code (default: eu)")
    p_refine.add_argument(
        "--min-overlap",
        type=float,
        default=0.5,
        help="minimum fraction of a basin that must lie inside the seed bbox (default: 0.5)",
    )
    p_refine.add_argument(
        "--no-clip",
        action="store_true",
        help="keep full basin extents instead of clipping to the seed bbox",
    )
    p_refine.set_defaults(func=cmd_refine_region)

    # --- v2 pipeline ---
    p_domain = sub.add_parser(
        "domain-boundary",
        help="build outer model domain from HydroBASINS union (plan §3.1)",
    )
    p_domain.add_argument("--level", type=int, default=7, help="HydroBASINS level for domain hull (default: 7)")
    p_domain.add_argument("--min-overlap", type=float, default=0.3)
    p_domain.set_defaults(func=cmd_domain_boundary)

    p_soiusa = sub.add_parser(
        "soiusa-groups",
        help="fetch or load SOIUSA/AVE mountain-group geometries (plan §3.1 Stufe 1)",
    )
    p_soiusa.add_argument(
        "--from-file",
        metavar="PATH",
        help="load from local GeoJSON/Shapefile instead of querying OSM Overpass",
    )
    p_soiusa.add_argument("--timeout", type=float, default=120.0, help="Overpass timeout in seconds")
    p_soiusa.set_defaults(func=cmd_soiusa_groups)

    p_v2 = sub.add_parser(
        "alpine-v2",
        help="SOIUSA-based region builder — Stufe 1 + 2 (plan §3.1 new approach)",
    )
    p_v2.add_argument("--level", type=int, default=8, help="HydroBASINS building-block level (default: 8)")
    p_v2.add_argument(
        "--groups",
        metavar="PATH",
        help="SOIUSA groups GeoJSON (default: data/regions/soiusa_groups.geojson)",
    )
    p_v2.add_argument(
        "--method",
        choices=["largest_overlap", "centroid"],
        default="largest_overlap",
        help="basin-to-group assignment method (default: largest_overlap)",
    )
    p_v2.set_defaults(func=cmd_alpine_v2)

    p_v2_dem = sub.add_parser(
        "alpine-v2-dem",
        help="download Copernicus tiles + build DEM mosaic for the v2 domain",
    )
    p_v2_dem.add_argument("--skip-download", action="store_true")
    p_v2_dem.add_argument("--skip-mosaic", action="store_true")
    p_v2_dem.set_defaults(func=cmd_alpine_v2_dem)

    p_v2_ahd = sub.add_parser(
        "alpine-v2-ahd",
        help="per-region AHD for v2 regions + optional edge threshold heights",
    )
    p_v2_ahd.add_argument("--force", action="store_true", help="recompute AHD even if NetCDF exists")
    p_v2_ahd.add_argument(
        "--edges",
        action="store_true",
        help="also compute region-boundary threshold heights (plan §3.2, §5.5)",
    )
    p_v2_ahd.add_argument(
        "--mosaic-path",
        metavar="PATH",
        help="explicit DEM mosaic (default: auto-detect alpine_v2→alpine_v0)",
    )
    p_v2_ahd.set_defaults(func=cmd_alpine_v2_ahd)

    p_v3 = sub.add_parser(
        "alpine-v3",
        help="optional §3.1.1 cross-segmentation applied to v2 regions",
    )
    p_v3.set_defaults(func=cmd_alpine_v3)

    p_build = sub.add_parser("build", help="fetch DEM + compute AHD NetCDF")
    p_build.add_argument("region")
    p_build.set_defaults(func=cmd_build)

    p_alpine = sub.add_parser(
        "alpine-v0",
        help="Sprint 1: clip HydroBASINS L8 to the Alpine bbox + tag (plan §3.1 Stufe 1)",
    )
    p_alpine.add_argument(
        "--min-overlap",
        type=float,
        default=0.5,
        help="minimum fraction of a basin inside ALPEN_BBOX (default: 0.5)",
    )
    p_alpine.set_defaults(func=cmd_alpine_v0)

    p_alpine_dem = sub.add_parser(
        "alpine-v0-dem",
        help="Sprint 2a: download all Copernicus tiles for the Alpine bbox + build mosaic",
    )
    p_alpine_dem.add_argument(
        "--skip-download", action="store_true", help="skip tile-download step"
    )
    p_alpine_dem.add_argument(
        "--skip-mosaic", action="store_true", help="skip mosaic-build step"
    )
    p_alpine_dem.set_defaults(func=cmd_alpine_v0_dem)

    p_alpine_ahd = sub.add_parser(
        "alpine-v0-ahd",
        help="Sprint 2b: per-region AHD against the shared mosaic + annotated GeoJSON",
    )
    p_alpine_ahd.add_argument(
        "--force", action="store_true", help="recompute AHD even if NetCDF exists"
    )
    p_alpine_ahd.set_defaults(func=cmd_alpine_v0_ahd)

    p_alpine_v1 = sub.add_parser(
        "alpine-v1",
        help="Sprint 3: §3.1.1 Quer-Segmentierung der Randbecken nach Höhenbändern",
    )
    p_alpine_v1.set_defaults(func=cmd_alpine_v1)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
