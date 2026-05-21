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
from pathlib import Path

import json

import geopandas as gpd
import shapely.geometry

from alptherm_icon.regions.ahd import compute_ahd
from alptherm_icon.regions.basins import fetch_hydrobasins, select_basins
from alptherm_icon.regions.dem import build_region_dem
from alptherm_icon.regions.polygon import load_region


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

    selected = select_basins(
        basins,
        seed_bbox,
        min_overlap_frac=args.min_overlap,
        apply_hauptkamm=not args.no_hauptkamm,
    )
    print(
        f"  selected {len(selected)} basins "
        f"(min_overlap={args.min_overlap}, hauptkamm={'off' if args.no_hauptkamm else 'on'})"
    )
    if selected.empty:
        raise RuntimeError(
            "no basins survived selection — try lowering --min-overlap or "
            "disabling --no-hauptkamm"
        )

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
        "hauptkamm_filter": (
            None if args.no_hauptkamm else "north_of_inn_linear"
        ),
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
        "--no-hauptkamm",
        action="store_true",
        help="skip the north-of-Inn hauptkamm filter",
    )
    p_refine.add_argument(
        "--no-clip",
        action="store_true",
        help="keep full basin extents instead of clipping to the seed bbox",
    )
    p_refine.set_defaults(func=cmd_refine_region)

    p_build = sub.add_parser("build", help="fetch DEM + compute AHD NetCDF")
    p_build.add_argument("region")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
