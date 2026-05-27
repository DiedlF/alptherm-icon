"""CLI for Komp. D circle/thermal detection (plan §6.2 + §6.3).

    # Detect thermals in one day's OGN log, assign to regions, write parquet
    python -m alptherm_icon.igc_pipeline detect --day 2026-05-27

Output: ``data/thermals/<day>_thermals.parquet`` — one row per detected
circling phase with centroid, climb rate, altitude band, region_id.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from alptherm_icon.igc_pipeline.circling import detect_thermals
from alptherm_icon.igc_pipeline.ogn_tracks import assemble_tracks


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


def _assign_regions(df, regions_path: Path):
    """Spatial-join thermal centroids to region_id (plan §6.3)."""
    import geopandas as gpd
    from shapely.geometry import Point

    if not regions_path.exists() or df.empty:
        df["region_id"] = None
        return df
    regions = gpd.read_file(regions_path)[["region_id", "habitat_class", "geometry"]]
    pts = gpd.GeoDataFrame(
        df.copy(),
        geometry=[Point(lon, lat) for lon, lat in zip(df["lon_centroid"], df["lat_centroid"])],
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(pts, regions, how="left", predicate="within")
    # A point on a shared boundary / in slightly-overlapping simplified
    # segments can match >1 region → sjoin emits duplicate rows. Keep the
    # first match per original point so lengths line up.
    joined = joined[~joined.index.duplicated(keep="first")].reindex(pts.index)
    df = df.copy()
    df["region_id"] = joined["region_id"].to_numpy()
    df["habitat_class"] = joined["habitat_class"].to_numpy()
    return df


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
            rows.append(
                {
                    "source_id": th.source_id,
                    "aircraft_type": th.aircraft_type,
                    "t_start": th.t_start,
                    "t_end": th.t_end,
                    "duration_s": th.duration_s,
                    "lat_centroid": th.lat_centroid,
                    "lon_centroid": th.lon_centroid,
                    "alt_mean_m": th.alt_mean_m,
                    "alt_base_m": th.alt_base_m,
                    "alt_top_m": th.alt_top_m,
                    "climb_rate_ms": th.climb_rate_ms,
                    "n_turns": th.n_turns,
                    "turn_sign": th.turn_sign,
                }
            )
    df = pd.DataFrame(rows)
    print(f"thermals detected: {len(df)}")
    if df.empty:
        print("(no thermals — nothing written)")
        return 0

    try:
        df = _assign_regions(df, root / "data" / "regions" / "alpine_v1_basins.geojson")
    except Exception as exc:  # noqa: BLE001 — never lose the parse to post-processing
        print(f"WARNING: region assignment failed ({exc!r}); writing without region_id")
        df["region_id"] = None
        df["habitat_class"] = None

    out_dir = root / "data" / "thermals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{day:%Y-%m-%d}_thermals.parquet"
    df.to_parquet(out)
    n_regions = df["region_id"].notna().sum()
    print(
        f"wrote {out.relative_to(root)} ({len(df)} thermals, "
        f"{n_regions} region-assigned)"
    )
    print(
        f"  climb_rate: median={df['climb_rate_ms'].median():.2f} "
        f"Q90={df['climb_rate_ms'].quantile(0.9):.2f} m/s"
    )
    print(
        f"  alt_top: median={df['alt_top_m'].median():.0f} "
        f"max={df['alt_top_m'].max():.0f} m"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.igc_pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser("detect", help="detect thermals in one day's OGN log")
    p_detect.add_argument("--day", required=True, help="YYYY-MM-DD (UTC)")
    p_detect.add_argument("--hour-lo", type=int, default=8, help="UTC window start (default 8)")
    p_detect.add_argument("--hour-hi", type=int, default=17, help="UTC window end (default 17)")
    p_detect.add_argument("--all-hours", action="store_true", help="ignore the hour window")
    p_detect.add_argument("--min-fixes", type=int, default=20)
    p_detect.add_argument("--max-aircraft", type=int, default=None, help="cap (dev)")
    p_detect.set_defaults(func=cmd_detect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
