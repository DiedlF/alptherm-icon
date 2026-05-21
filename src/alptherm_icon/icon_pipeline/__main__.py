"""CLI for Komp. B — DWD ICON-D2 surface fetch.

    python -m alptherm_icon.icon_pipeline fetch <region> --init <YYYYMMDDHH> \\
        [--vars t_2m,asob_s] [--lead-max 48]

Reads the region polygon from `configs/regions/<region>.geojson`,
downloads ICON-D2 GRIB2 surface files for each (var, lead) into
`data/icon/grib/`, extracts a region-centroid + region-polygon-mean
time series, and writes `data/icon/<region>_<init>.nc`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from alptherm_icon.icon_pipeline.icon import fetch_surface_series
from alptherm_icon.regions.polygon import load_region


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def _config_path(root: Path, region: str) -> Path:
    path = root / "configs" / "regions" / f"{region}.geojson"
    if not path.exists():
        raise FileNotFoundError(f"no region config at {path}")
    return path


def cmd_fetch(args: argparse.Namespace) -> int:
    root = _project_root()
    geom, props = load_region(_config_path(root, args.region), name=args.region)
    init = datetime.strptime(args.init, "%Y%m%d%H")
    variables = [v.strip().lower() for v in args.vars.split(",")]
    print(
        f"region={args.region!r} init={init:%Y-%m-%dT%H:%MZ} "
        f"vars={variables} lead_max={args.lead_max}"
    )

    cache_dir = root / "data" / "icon" / "grib"
    ds = fetch_surface_series(
        region_geom=geom,
        region_name=args.region,
        init=init,
        variables=variables,
        cache_dir=cache_dir,
        lead_max=args.lead_max,
    )

    out_dir = root / "data" / "icon"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.region}_{init:%Y%m%d%H}.nc"
    ds.to_netcdf(out)
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e3:.1f} kB)")
    n_missing = sum(
        int(bool((ds[v].isnull()).any().item())) for v in ds.data_vars
    )
    print(
        f"  vars: {list(ds.data_vars)}; "
        f"time: {ds.time.size} steps; "
        f"any-missing vars: {n_missing}/{len(ds.data_vars)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.icon_pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="download ICON-D2 surface series for a region")
    p_fetch.add_argument("region", help="region name (configs/regions/<name>.geojson)")
    p_fetch.add_argument(
        "--init", required=True, help="init time UTC, format YYYYMMDDHH (hour in {00,03,06,09,12,15,18,21})"
    )
    p_fetch.add_argument(
        "--vars",
        default="t_2m,asob_s",
        help="comma-separated DWD variable tokens (default: t_2m,asob_s)",
    )
    p_fetch.add_argument(
        "--lead-max",
        type=int,
        default=48,
        help="max forecast hour, inclusive (default: 48)",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
