"""CLI for Komp. D circle/thermal detection (plan §6.2 + §6.3).

    # Detect thermals in one day's OGN log, write parquet
    python -m alptherm_icon.igc_pipeline detect --day 2026-05-27

    # Process all archived OGN days that have no parquet yet
    python -m alptherm_icon.igc_pipeline backfill

    # Re-run spatial region assignment on existing parquets (after new regions)
    python -m alptherm_icon.igc_pipeline reassign

Stage 1 (raw): track assembly + circle detection → parquet with centroids
and climb rates, region_id NULL when no regions are built yet.

Stage 2 (spatial): ``reassign`` replays the spatial join once stable regions
exist, in place on the existing parquets — no re-parsing of OGN logs needed.

Regions are auto-detected in preference order:
  alpine_v2_regions_annotated.geojson  (v2 post-AHD, best)
  alpine_v2_regions.geojson            (v2 pre-AHD)
  alpine_v1_basins.geojson             (v1 fallback)
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from alptherm_icon.igc_pipeline.circling import detect_thermals
from alptherm_icon.igc_pipeline.ogn_tracks import assemble_tracks

log = logging.getLogger(__name__)


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / "pyproject.toml").exists():
            return c
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def _ogn_log_path(root: Path, day: dt.date) -> Path:
    return (
        root / "data" / "ogn" / "raw" / f"{day.year:04d}" / f"{day.month:02d}"
        / f"{day:%Y-%m-%d}.jsonl.gz"
    )


def _best_regions_path(root: Path) -> Path | None:
    """Auto-detect the most up-to-date regions file."""
    for candidate in (
        root / "data" / "regions" / "alpine_v2_regions_annotated.geojson",
        root / "data" / "regions" / "alpine_v2_regions.geojson",
        root / "data" / "regions" / "alpine_v1_basins.geojson",
    ):
        if candidate.exists():
            return candidate
    return None


def _assign_regions(
    df,
    root: Path,
    regions_path: Path | None = None,
):
    """Spatial-join thermal centroids to region_id + terrain class (plan §6.3).

    Returns (df_with_region_cols, regions_path_used | None).
    Auto-detects the best available regions file when regions_path is None.
    Supports both v2 (terrain_type) and v1 (habitat_class) column schemas.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    if regions_path is None:
        regions_path = _best_regions_path(root)

    if regions_path is None or not regions_path.exists() or df.empty:
        df = df.copy()
        df["region_id"] = None
        return df, None

    gdf = gpd.read_file(regions_path)
    keep = ["region_id", "geometry"]
    for col in ("terrain_type", "habitat_class"):
        if col in gdf.columns:
            keep.append(col)
    regions = gdf[[c for c in keep if c in gdf.columns]]

    pts = gpd.GeoDataFrame(
        df.copy(),
        geometry=[Point(lon, lat) for lon, lat in zip(df["lon_centroid"], df["lat_centroid"])],
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(pts, regions, how="left", predicate="within")
    # A centroid on a shared boundary can match > 1 region — keep first.
    joined = joined[~joined.index.duplicated(keep="first")].reindex(pts.index)
    df = df.copy()
    df["region_id"] = joined["region_id"].to_numpy()
    for col in ("terrain_type", "habitat_class"):
        if col in joined.columns:
            df[col] = joined[col].to_numpy()
    return df, regions_path


def _thermals_path(root: Path, day: dt.date) -> Path:
    return root / "data" / "thermals" / f"{day:%Y-%m-%d}_thermals.parquet"


def cmd_detect(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    import pandas as pd

    root = _project_root()
    day = dt.date.fromisoformat(args.day)
    log_path = _ogn_log_path(root, day)
    if not log_path.exists():
        print(f"ERROR: no OGN log at {log_path.relative_to(root)}", file=sys.stderr)
        return 2

    window = None if args.all_hours else (args.hour_lo, args.hour_hi)
    tracks = assemble_tracks(
        log_path,
        hour_window=window,
        min_fixes=args.min_fixes,
        max_aircraft=args.max_aircraft,
    )
    print(f"tracks: {len(tracks)}")

    rows = []
    for tr in tracks:
        for th in detect_thermals(tr):
            rows.append({
                "source_id":    th.source_id,
                "aircraft_type": th.aircraft_type,
                "t_start":      th.t_start,
                "t_end":        th.t_end,
                "duration_s":   th.duration_s,
                "lat_centroid": th.lat_centroid,
                "lon_centroid": th.lon_centroid,
                "alt_mean_m":   th.alt_mean_m,
                "alt_base_m":   th.alt_base_m,
                "alt_top_m":    th.alt_top_m,
                "climb_rate_ms": th.climb_rate_ms,
                "n_turns":      th.n_turns,
                "turn_sign":    th.turn_sign,
            })
    df = pd.DataFrame(rows)
    print(f"thermals detected: {len(df)}")

    if df.empty:
        # Write an empty parquet so backfill knows this day was attempted.
        out_dir = root / "data" / "thermals"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = _thermals_path(root, day)
        df.to_parquet(out)
        print(f"wrote {out.relative_to(root)} (0 thermals)")
        return 0

    regions_path = None if args.no_regions else (
        Path(args.regions) if args.regions else None
    )
    try:
        df, used = _assign_regions(df, root, regions_path=regions_path)
        n_assigned = int(df["region_id"].notna().sum())
        used_name = used.name if used else "none"
        print(f"region-assigned: {n_assigned}/{len(df)} (from {used_name})")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: region assignment failed ({exc!r}); writing without region_id")
        df["region_id"] = None

    out_dir = root / "data" / "thermals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = _thermals_path(root, day)
    df.to_parquet(out)
    print(f"wrote {out.relative_to(root)} ({len(df)} thermals)")
    if not df.empty and "climb_rate_ms" in df.columns:
        print(
            f"  climb_rate: median={df['climb_rate_ms'].median():.2f} "
            f"Q90={df['climb_rate_ms'].quantile(0.9):.2f} m/s"
        )
        print(
            f"  alt_top: median={df['alt_top_m'].median():.0f} "
            f"max={df['alt_top_m'].max():.0f} m"
        )
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Process all archived OGN days that have no thermals parquet yet."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    root = _project_root()
    ogn_root = root / "data" / "ogn" / "raw"
    thermals_dir = root / "data" / "thermals"

    # All available OGN log files, sorted chronologically.
    all_logs = sorted(ogn_root.rglob("????-??-??.jsonl.gz"))
    if not all_logs:
        print("no OGN logs found under data/ogn/raw/")
        return 0

    # Days that already have a parquet (including empty ones from prior runs).
    existing = {
        p.name.replace("_thermals.parquet", "")
        for p in thermals_dir.glob("*_thermals.parquet")
    }

    pending = [
        log_path.stem.replace(".jsonl", "")   # "YYYY-MM-DD"
        for log_path in all_logs
        if log_path.stem.replace(".jsonl", "") not in existing
    ]

    if args.days:
        pending = pending[-args.days:]

    if not pending:
        print("all archived OGN days already processed — nothing to do")
        return 0

    print(f"backfill: {len(pending)} days to process")
    for day_str in pending:
        print(f"  → {day_str}")

    if args.dry_run:
        return 0

    errors: list[str] = []
    for day_str in pending:
        ns = argparse.Namespace(
            day=day_str,
            hour_lo=7,
            hour_hi=18,
            all_hours=False,
            min_fixes=20,
            max_aircraft=None,
            no_regions=args.no_regions,
            regions=args.regions,
        )
        try:
            rc = cmd_detect(ns)
            if rc != 0:
                errors.append(day_str)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {day_str}: {exc!r}", file=sys.stderr)
            errors.append(day_str)

    if errors:
        print(f"backfill: {len(errors)} failed: {errors}", file=sys.stderr)
        return 1
    print(f"backfill: done — {len(pending)} days processed")
    return 0


def cmd_reassign(args: argparse.Namespace) -> int:
    """Re-run spatial region assignment on all existing thermals parquets.

    Use this after building stable v2 regions to update region_id without
    re-parsing OGN logs. Overwrites parquets in place.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    import pandas as pd

    root = _project_root()
    thermals_dir = root / "data" / "thermals"

    parquets = sorted(thermals_dir.glob("*_thermals.parquet"))
    if not parquets:
        print("no thermals parquets found — run backfill first")
        return 0

    regions_path = Path(args.regions) if args.regions else None
    if regions_path is None:
        regions_path = _best_regions_path(root)
    if regions_path is None:
        print("ERROR: no regions file found; run Komp. A pipeline first", file=sys.stderr)
        return 2

    print(f"reassigning {len(parquets)} parquets using {regions_path.name}")

    errors: list[str] = []
    for parquet in parquets:
        day_str = parquet.name.replace("_thermals.parquet", "")
        try:
            df = pd.read_parquet(parquet)
            if df.empty:
                print(f"  {day_str}: 0 thermals, skip")
                continue
            # Drop old region columns so the join is clean regardless of schema.
            df = df.drop(columns=["region_id", "terrain_type", "habitat_class"],
                          errors="ignore")
            df, used = _assign_regions(df, root, regions_path=regions_path)
            df.to_parquet(parquet)
            n = int(df["region_id"].notna().sum())
            print(f"  {day_str}: {n}/{len(df)} assigned")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {day_str}: {exc!r}", file=sys.stderr)
            errors.append(day_str)

    if errors:
        print(f"reassign: {len(errors)} failed: {errors}", file=sys.stderr)
        return 1
    print("reassign: done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.igc_pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- detect (single day) ---
    p_detect = sub.add_parser("detect", help="detect thermals in one day's OGN log")
    p_detect.add_argument("--day", required=True, help="YYYY-MM-DD (UTC)")
    p_detect.add_argument("--hour-lo", type=int, default=7,
                          help="UTC convective window start (default 7)")
    p_detect.add_argument("--hour-hi", type=int, default=18,
                          help="UTC convective window end (default 18)")
    p_detect.add_argument("--all-hours", action="store_true",
                          help="ignore the hour window")
    p_detect.add_argument("--min-fixes", type=int, default=20)
    p_detect.add_argument("--max-aircraft", type=int, default=None,
                          help="cap for dev/testing")
    p_detect.add_argument("--no-regions", action="store_true",
                          help="skip spatial region assignment (raw mode, stage 1 only)")
    p_detect.add_argument("--regions", metavar="PATH",
                          help="explicit regions GeoJSON (default: auto-detect v2→v1)")
    p_detect.set_defaults(func=cmd_detect)

    # --- backfill (all unprocessed days) ---
    p_bf = sub.add_parser(
        "backfill",
        help="process all archived OGN days that have no thermals parquet yet",
    )
    p_bf.add_argument("--days", type=int, default=None, metavar="N",
                      help="limit to the last N pending days (default: all)")
    p_bf.add_argument("--dry-run", action="store_true",
                      help="list pending days without processing")
    p_bf.add_argument("--no-regions", action="store_true",
                      help="skip region assignment (stage 1 only — run reassign later)")
    p_bf.add_argument("--regions", metavar="PATH",
                      help="explicit regions GeoJSON")
    p_bf.set_defaults(func=cmd_backfill)

    # --- reassign (spatial join replay) ---
    p_ra = sub.add_parser(
        "reassign",
        help="re-run region spatial join on all existing parquets (stage 2)",
    )
    p_ra.add_argument("--regions", metavar="PATH",
                      help="explicit regions GeoJSON (default: auto-detect v2→v1)")
    p_ra.set_defaults(func=cmd_reassign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
