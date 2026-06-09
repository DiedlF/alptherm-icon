"""CLI for Komp. C — the convection kernel.

    python -m alptherm_icon.model run <region> --init <YYYYMMDDHH> \\
        [--model bulk|parcel] [--flux-source proxy|icon] \\
        [--lead-start 6] [--lead-end 12] [--sensible-fraction 0.3] [--v-sub 0]

Reads the morning profile NetCDF from Komp. B
(`data/icon/<region>_<init>_lead<NNN>_profile.nc`) and the surface
time series (`data/icon/<region>_<init>.nc`) and evolves the column from
``lead_start`` to ``lead_end``.

``--model bulk`` (default): v0.1/v0.2 mixed-layer encroachment → ``cbl_top``,
``theta_mixed``, ``w_star``. ``--model parcel``: v0.3 bin-wise parcel theory
(needs the region AHD at ``data/regions/<region>_ahd.nc``) → ``cbl_top``,
``v_max``, ``cloud_base/top``, ``cloud_cover_octas``.

Surface sensible-heat flux comes from one of two sources (``--flux-source``):
``icon`` uses ASHFL_S directly (sign-flipped to model-positive); ``proxy``
(default) uses ASOB_S × ``sensible_fraction``. Both are de-averaged from ICON's
mean-since-init convention first (see ``model/forcing.py``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import xarray as xr

from alptherm_icon.model import forcing
from alptherm_icon.model import parcel as parcel_model
from alptherm_icon.model import thermo as th
from alptherm_icon.model.mixed_layer import evolve_mixed_layer
from alptherm_icon.model.thermo import potential_temperature, standard_pressure
from alptherm_icon.regions.ahd import AHDProfile


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

    if args.model == "parcel":
        return _run_parcel(args, root, init, profile, surface, profile_path, surface_path)

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

    # De-average surface fluxes over the FULL series (lead 0..end) *before*
    # windowing: ICON surface fluxes are means-since-init, so recovering the
    # interval-mean flux needs the cumulative integral back to lead 0.
    full_times = surface.time.values
    lead_s = ((full_times - np.datetime64(init)) / np.timedelta64(1, "s")).astype(float)

    if args.flux_source == "icon":
        if "ashfl_s_mean" not in surface:
            raise RuntimeError(
                f"--flux-source icon needs 'ashfl_s_mean' in {surface_path.name}; "
                "re-fetch with: python -m alptherm_icon.icon_pipeline fetch "
                f"{args.region} --init {args.init} "
                "--vars t_2m,asob_s,athb_s,ashfl_s,alhfl_s,t_g"
            )
        sensible_full = forcing.turbulent_flux_from_icon(
            surface["ashfl_s_mean"].values, lead_s
        )
        flux_long_name = "−ASHFL_S de-averaged (model-positive sensible flux)"
    else:
        sensible_full = forcing.sensible_flux_proxy(
            surface["asob_s_mean"].values, lead_s, args.sensible_fraction
        )
        flux_long_name = f"ASOB_S de-averaged × {args.sensible_fraction} (proxy)"

    # Window to [lead_start, lead_end] on the de-averaged series.
    flux_da = xr.DataArray(sensible_full, coords={"time": full_times}, dims="time")
    t_start = np.datetime64(init + timedelta(hours=args.lead_start))
    t_end = np.datetime64(init + timedelta(hours=args.lead_end))
    sfc_window = flux_da.sel(time=slice(t_start, t_end))
    if sfc_window.time.size < 2:
        raise RuntimeError(
            "need at least 2 surface time steps in window; "
            "extend Komp. B fetch with a larger --lead-max"
        )
    times = sfc_window.time.values
    sensible_flux = sfc_window.values
    dt_s = float((times[1] - times[0]) / np.timedelta64(1, "s"))
    print(
        f"forcing: source={args.flux_source}, {times.size} steps × {dt_s:.0f} s, "
        f"sensible flux range [{np.nanmin(sensible_flux):.1f}, "
        f"{np.nanmax(sensible_flux):.1f}] W/m²"
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
                {"units": "W/m2", "long_name": flux_long_name},
            ),
        },
        coords={"time": ("time", times)},
        attrs={
            "region_name": args.region,
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile_source": str(profile_path.relative_to(root)),
            "surface_source": str(surface_path.relative_to(root)),
            "flux_source": args.flux_source,
            "sensible_fraction": float(args.sensible_fraction),
            "model_version": "C-v0.2-bulk-mixed-layer",
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


def _load_ahd(root: Path, region: str) -> AHDProfile:
    path = root / "data" / "regions" / f"{region}_ahd.nc"
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path.relative_to(root)} — build the region AHD first "
            f"(Komp. A), e.g.: python -m alptherm_icon.regions build {region}"
        )
    ds = xr.open_dataset(path)
    return AHDProfile(
        region_name=region,
        z_bottom_m=ds["z_bottom"].values,
        z_top_m=ds["z_top"].values,
        s_g=ds["s_g"].values,
        v_a=ds["v_a"].values,
        region_area_m2=float(ds.attrs["region_area_m2"]),
    )


def _run_parcel(
    args: argparse.Namespace,
    root: Path,
    init: datetime,
    profile: xr.Dataset,
    surface: xr.Dataset,
    profile_path: Path,
    surface_path: Path,
) -> int:
    """Komp. C v0.3 — bin-wise parcel theory driven by ICON forcing + AHD."""
    ahd = _load_ahd(root, args.region)

    # Morning sounding from the ICON profile: T and Td (from qv) vs height.
    z = profile["height"].values
    t = profile["t"].values
    order = np.argsort(z)
    z, t = z[order], t[order]
    p = np.asarray(standard_pressure(z))
    if "qv" in profile:
        qv = profile["qv"].values[order]
        r = qv / np.clip(1.0 - qv, 1e-9, None)  # specific humidity → mixing ratio
        e_hpa = r * p / 100.0 / (th.EPSILON + r)
        td = np.asarray(th.dewpoint_from_vapor_pressure(e_hpa))
    else:
        td = t - 5.0  # crude fallback if humidity wasn't fetched
    grid = parcel_model.build_grid(z, t, td, ahd)
    print(
        f"parcel grid: {grid.z_center_m.size} layers, "
        f"z=[{grid.z_center_m[0]:.0f}, {grid.z_center_m[-1]:.0f}] m; "
        f"AHD region area {ahd.region_area_m2 / 1e6:.0f} km²"
    )

    # ICON surface forcing (de-averaged), windowed to [lead_start, lead_end].
    full_times = surface.time.values
    lead_s = ((full_times - np.datetime64(init)) / np.timedelta64(1, "s")).astype(float)
    p_lat_full = np.zeros_like(lead_s)
    if args.flux_source == "icon" and "ashfl_s_mean" in surface:
        p_sens_full = forcing.turbulent_flux_from_icon(surface["ashfl_s_mean"].values, lead_s)
        if "alhfl_s_mean" in surface:
            p_lat_full = forcing.turbulent_flux_from_icon(surface["alhfl_s_mean"].values, lead_s)
        src = "icon (−ASHFL_S/−ALHFL_S)"
    else:
        p_sens_full = forcing.sensible_flux_proxy(
            surface["asob_s_mean"].values, lead_s, args.sensible_fraction
        )
        src = f"proxy (ASOB_S × {args.sensible_fraction})"
    t_start = np.datetime64(init + timedelta(hours=args.lead_start))
    t_end = np.datetime64(init + timedelta(hours=args.lead_end))

    def _window(arr: np.ndarray) -> np.ndarray:
        da = xr.DataArray(arr, coords={"time": full_times}, dims="time")
        return np.asarray(da.sel(time=slice(t_start, t_end)).values, dtype=np.float64)

    times = xr.DataArray(p_sens_full, coords={"time": full_times}, dims="time").sel(
        time=slice(t_start, t_end)
    ).time.values
    p_sens_series = _window(p_sens_full)
    p_lat_series = _window(p_lat_full)
    if times.size < 2:
        raise RuntimeError("need at least 2 surface steps in window")
    dt_s = float((times[1] - times[0]) / np.timedelta64(1, "s"))
    print(
        f"forcing: {src}, {times.size} steps × {dt_s:.0f} s, "
        f"P_sens range [{np.nanmin(p_sens_series):.1f}, {np.nanmax(p_sens_series):.1f}] W/m²; "
        f"v_sub={args.v_sub} m/h"
    )

    def forcing_fn(s: int, _T_surf: float, _Td_surf: float) -> tuple[float, float]:
        return float(max(p_sens_series[s], 0.0)), float(max(p_lat_series[s], 0.0))

    steps = parcel_model.run_day(
        grid, forcing_fn, len(times), dt_s,
        u_km_h=args.wind_km_h, w_sub_m_s=args.v_sub / 3600.0,
    )

    def _col(attr: str) -> np.ndarray:
        return np.array([getattr(s, attr) for s in steps], dtype=np.float64)

    # v(z, t): per-layer updraft speed, the headline lift-rate field (plan §5.3).
    v_zt = np.vstack([s.v_profile_m_s for s in steps])  # (time, z)

    out_ds = xr.Dataset(
        data_vars={
            "v": (
                ("time", "z"), v_zt,
                {"units": "m/s", "long_name": "updraft speed (100 m × step bins)"},
            ),
            "cbl_top": ("time", _col("z_i_m"), {"units": "m", "long_name": "mixed-layer top (MSL)"}),
            "v_max": ("time", _col("v_max_m_s"), {"units": "m/s", "long_name": "peak updraft speed"}),
            "cloud_base": ("time", _col("cloud_base_m"), {"units": "m", "long_name": "cumulus base (MSL)"}),
            "cloud_top": ("time", _col("cloud_top_m"), {"units": "m", "long_name": "cumulus top (MSL)"}),
            "cloud_cover_octas": ("time", _col("cloud_cover_octas"), {"units": "octas", "long_name": "cloud cover"}),
        },
        coords={"time": ("time", times), "z": ("z", grid.z_center_m, {"units": "m", "long_name": "height MSL"})},
        attrs={
            "region_name": args.region,
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile_source": str(profile_path.relative_to(root)),
            "surface_source": str(surface_path.relative_to(root)),
            "flux_source": args.flux_source,
            "v_sub_m_per_h": float(args.v_sub),
            "model_version": "C-v0.3-parcel",
        },
    )
    out_dir = root / "data" / "model"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.region}_{init:%Y%m%d%H}_parcel.nc"
    out_ds.to_netcdf(out)
    print(f"wrote {out.relative_to(root)} ({out.stat().st_size / 1e3:.1f} kB)")
    zi = _col("z_i_m")
    cb = _col("cloud_base_m")
    cloudy = np.isfinite(cb)
    onset = str(times[cloudy.argmax()])[:16] if cloudy.any() else "none"
    print(
        f"  CBL top: start {zi[0]:.0f} m → max {zi.max():.0f} m; "
        f"v_max {out_ds['v_max'].max().item():.2f} m/s; "
        f"cloud onset {onset}, max cover {out_ds['cloud_cover_octas'].max().item():.1f} octas"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.model")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="evolve the convection model for a region/init")
    p_run.add_argument("region")
    p_run.add_argument("--init", required=True, help="init time UTC, YYYYMMDDHH")
    p_run.add_argument(
        "--model", choices=("bulk", "parcel"), default="bulk",
        help="'bulk' = v0.1/v0.2 mixed-layer encroachment (default, fast); "
        "'parcel' = v0.3 bin-wise parcel theory (needs the region AHD)",
    )
    p_run.add_argument(
        "--v-sub", type=float, default=0.0,
        help="large-scale subsidence rate [m/h] for the parcel model (default: 0)",
    )
    p_run.add_argument(
        "--wind-km-h", type=float, default=0.0,
        help="mean wind [km/h] for the parcel model's wind reduction (eq 19; default: 0)",
    )
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
        "--flux-source", choices=("proxy", "icon"), default="proxy",
        help="surface sensible-flux source: 'proxy' = ASOB_S × sensible_fraction "
        "(v0.1, default), 'icon' = −ASHFL_S direct (v0.2, needs ashfl_s fetched)",
    )
    p_run.add_argument(
        "--sensible-fraction", type=float, default=0.3,
        help="fraction of ASOB_S used as sensible-heat flux proxy "
        "(default: 0.3; ignored when --flux-source icon)",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
