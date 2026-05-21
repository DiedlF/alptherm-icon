"""CLI for Komp. C v0.1 — bulk mixed-layer evolution.

    python -m alptherm_icon.model run <region> --init <YYYYMMDDHH> \\
        [--lead-start 6] [--lead-end 12] [--sensible-fraction 0.3]

Reads the morning profile NetCDF from Komp. B
(`data/icon/<region>_<init>_lead<NNN>_profile.nc`) and the surface
time series (`data/icon/<region>_<init>.nc`), evolves the bulk
mixed layer forward from ``lead_start`` to ``lead_end`` at hourly
steps using ICON ASOB_S × ``sensible_fraction`` as the surface
sensible-heat-flux proxy, and writes a NetCDF with the diurnal
``cbl_top``, ``theta_mixed``, ``w_star`` series.

Surface sensible heat flux is approximated as a constant fraction of
net surface SW. ICON ICON-D2 ships ASHFL_S (true sensible heat flux)
as well — switching to that is a v0.2 ergonomic improvement.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import xarray as xr

from alptherm_icon.model.mixed_layer import evolve_mixed_layer
from alptherm_icon.model.thermo import potential_temperature, standard_pressure


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / "pyproject.toml").exists():
            return c
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def cmd_run(args: argparse.Namespace) -> int:
    root = _project_root()
    init = datetime.strptime(args.init, "%Y%m%d%H")

    profile_path = (
        root / "data" / "icon"
        / f"{args.region}_{init:%Y%m%d%H}_lead{args.lead_profile:03d}_profile.nc"
    )
    surface_path = root / "data" / "icon" / f"{args.region}_{init:%Y%m%d%H}.nc"
    for p in (profile_path, surface_path):
        if not p.exists():
            raise FileNotFoundError(
                f"missing {p.relative_to(root)} — run the Komp. B fetchers first"
            )

    profile = xr.open_dataset(profile_path)
    surface = xr.open_dataset(surface_path)

    # Build the morning sounding sorted surface-up.
    z = profile["height"].values
    t = profile["t"].values
    order = np.argsort(z)
    z = z[order]
    t = t[order]
    p = standard_pressure(z)
    theta = potential_temperature(t, p)
    print(
        f"profile: {z.size} levels, z=[{z[0]:.0f}, {z[-1]:.0f}] m, "
        f"surface T={t[0]:.1f} K, θ(z_sfc)={theta[0]:.1f} K"
    )

    # Slice the surface series to [lead_start, lead_end].
    t_start = np.datetime64(init + timedelta(hours=args.lead_start))
    t_end = np.datetime64(init + timedelta(hours=args.lead_end))
    sfc_window = surface.sel(time=slice(t_start, t_end))
    if sfc_window.time.size < 2:
        raise RuntimeError(
            "need at least 2 surface time steps in window; "
            "extend Komp. B fetch with a larger --lead-max"
        )
    times = sfc_window.time.values
    asob_s = sfc_window["asob_s_mean"].values
    sensible_flux = asob_s * args.sensible_fraction
    dt_s = float((times[1] - times[0]) / np.timedelta64(1, "s"))
    print(
        f"forcing: {times.size} steps × {dt_s:.0f} s, "
        f"ASOB_S range [{asob_s.min():.1f}, {asob_s.max():.1f}] W/m², "
        f"sensible fraction = {args.sensible_fraction}"
    )

    steps = evolve_mixed_layer(theta, z, sensible_flux, dt_s)

    out_ds = xr.Dataset(
        data_vars={
            "cbl_top": (
                "time",
                np.array([s.z_i_m for s in steps]),
                {"units": "m", "long_name": "CBL top above MSL (encroachment)"},
            ),
            "cbl_depth": (
                "time",
                np.array([s.z_i_m - float(z[0]) for s in steps]),
                {"units": "m", "long_name": "CBL depth above the surface model level"},
            ),
            "theta_mixed": (
                "time",
                np.array([s.theta_m_K for s in steps]),
                {"units": "K", "long_name": "mixed-layer potential temperature"},
            ),
            "w_star": (
                "time",
                np.array([s.w_star_m_s for s in steps]),
                {"units": "m/s", "long_name": "Deardorff convective velocity scale"},
            ),
            "h_cum": (
                "time",
                np.array([s.h_cum_j_m2 for s in steps]),
                {"units": "J/m2", "long_name": "cumulative surface sensible heat input"},
            ),
            "sensible_flux": (
                "time",
                sensible_flux,
                {"units": "W/m2", "long_name": "ASOB_S × sensible_fraction (proxy)"},
            ),
        },
        coords={"time": ("time", times)},
        attrs={
            "region_name": args.region,
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile_source": str(profile_path.relative_to(root)),
            "surface_source": str(surface_path.relative_to(root)),
            "sensible_fraction": float(args.sensible_fraction),
            "model_version": "C-v0.1-bulk-mixed-layer",
        },
    )
    out_dir = root / "data" / "model"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.region}_{init:%Y%m%d%H}_cbl.nc"
    out_ds.to_netcdf(out)
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e3:.1f} kB)")
    z_i_above = out_ds["cbl_depth"].values
    print(
        f"  CBL depth: start {z_i_above[0]:.0f} m -> end {z_i_above[-1]:.0f} m "
        f"(max {z_i_above.max():.0f} m at {str(times[z_i_above.argmax()])[:16]})"
    )
    print(
        f"  w*: max {out_ds['w_star'].max().item():.2f} m/s; "
        f"θ_m gain: {steps[-1].theta_m_K - steps[0].theta_m_K:.2f} K"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.model")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="evolve bulk mixed layer for a region/init")
    p_run.add_argument("region")
    p_run.add_argument("--init", required=True, help="init time UTC, YYYYMMDDHH")
    p_run.add_argument(
        "--lead-profile", type=int, default=6,
        help="forecast hour the morning profile was saved at (default: 6)",
    )
    p_run.add_argument(
        "--lead-start", type=int, default=6,
        help="forecast hour to start the mixed-layer integration (default: 6)",
    )
    p_run.add_argument(
        "--lead-end", type=int, default=12,
        help="forecast hour to stop (inclusive, default: 12)",
    )
    p_run.add_argument(
        "--sensible-fraction", type=float, default=0.3,
        help="fraction of ASOB_S to use as sensible-heat flux proxy (default: 0.3)",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
