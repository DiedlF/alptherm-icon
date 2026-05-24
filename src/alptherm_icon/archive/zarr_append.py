"""Tier-1 GRIB2 → daily Zarr append (plan §9.4).

For each archive run we have one GRIB2 file per (variable, lead). Reading
those files back from disk to answer "what was ASOB_S at 11 UTC on
2026-05-23?" is painful — every query reopens 49 small GRIB files.

The Zarr layout is one chunked array per variable on the Alpen-Bbox
grid::

    tier1.zarr/
        time          (T,)              datetime64
        latitude      (Y,)              float64
        longitude     (X,)              float64
        t_2m          (T, Y, X)         float32  chunked (24, Y, X)
        asob_s        (T, Y, X)         float32
        ...

We append along the ``time`` dim per run. Idempotency is enforced at
the orchestrator level (manifest check), not here — re-appending the
same init duplicates time-stamps.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import xarray as xr

from alptherm_icon.archive.bbox import ALPEN_BBOX, subset_to_bbox

log = logging.getLogger(__name__)


def _open_single(path: Path, step_h: int) -> xr.Dataset:
    """Open one GRIB2 file and collapse it to a 2D field for ``step_h``.

    Some ICON-D2 variables (cape_ml, tot_prec, hbas_sc, htop_sc, …) are
    published with 15-minute sub-steps packed into the hourly file, so a
    naive open returns ``(step=4, lat, lon)``. Filtering on ``step`` =
    ``step_h`` hours selects the full-hour message and yields a 2D
    array that aligns with the other variables on the Zarr time axis.
    """
    return xr.open_dataset(
        path,
        engine="cfgrib",
        backend_kwargs={"indexpath": "", "filter_by_keys": {"step": float(step_h)}},
    )


def _build_dataset(
    grib_paths: dict[tuple[str, int], Path],
    init: dt.datetime,
) -> xr.Dataset | None:
    """Stack all (var, lead) GRIB2s into one (time, lat, lon) Dataset on the bbox grid.

    Returns ``None`` if no usable files were found.
    """
    # Group paths by lead so we open one (lead × all vars) bundle at a time
    # and align them on the same target lat/lon.
    leads = sorted({lead for _, lead in grib_paths})
    if not leads:
        return None

    # Pin the target grid from the first successfully-opened lead-0 file
    # (or whichever lead exists). Subsequent files are reindexed to match.
    target_lat: np.ndarray | None = None
    target_lon: np.ndarray | None = None
    lat_name: str | None = None
    lon_name: str | None = None

    per_var_arrays: dict[str, list[np.ndarray]] = {}
    times: list[np.datetime64] = []

    init_naive = init.replace(tzinfo=None) if init.tzinfo is not None else init

    for lead in leads:
        time_val = np.datetime64(init_naive) + np.timedelta64(lead, "h")
        lead_vars: dict[str, np.ndarray] = {}
        for (var, lead_h), path in grib_paths.items():
            if lead_h != lead:
                continue
            try:
                ds = _open_single(path, step_h=lead_h)
            except Exception as exc:  # noqa: BLE001 — skip bad files, keep going
                log.warning("zarr: failed to open %s: %r", path.name, exc)
                continue
            try:
                ds_sub = subset_to_bbox(ds, ALPEN_BBOX)
                if target_lat is None:
                    lat_name = next(n for n in ("latitude", "lat", "y") if n in ds_sub.coords)
                    lon_name = next(n for n in ("longitude", "lon", "x") if n in ds_sub.coords)
                    target_lat = ds_sub[lat_name].values
                    target_lon = ds_sub[lon_name].values
                data_var = next(iter(ds_sub.data_vars))
                arr = ds_sub[data_var].values
                if arr.shape != (target_lat.size, target_lon.size):
                    # Should be unreachable after the step filter — log loudly if it isn't.
                    log.warning(
                        "zarr: skipping %s — shape %s != target %s (multi-step file?)",
                        path.name,
                        arr.shape,
                        (target_lat.size, target_lon.size),
                    )
                    continue
                lead_vars[var] = arr.astype(np.float32, copy=False)
            finally:
                ds.close()
        if not lead_vars:
            continue
        times.append(time_val)
        for var, arr in lead_vars.items():
            per_var_arrays.setdefault(var, []).append(arr)

    if not times or target_lat is None or target_lon is None:
        return None

    n_t = len(times)
    data_vars: dict[str, tuple] = {}
    for var, frames in per_var_arrays.items():
        # If a var is missing for some leads, pad with NaN so the time axis aligns.
        if len(frames) != n_t:
            padded = np.full(
                (n_t, target_lat.size, target_lon.size), np.nan, dtype=np.float32
            )
            for i in range(min(len(frames), n_t)):
                padded[i] = frames[i]
            stack = padded
        else:
            stack = np.stack(frames, axis=0)
        data_vars[var] = (("time", "latitude", "longitude"), stack)

    return xr.Dataset(
        data_vars=data_vars,
        coords={
            "time": np.asarray(times, dtype="datetime64[ns]"),
            "latitude": target_lat.astype(np.float64),
            "longitude": target_lon.astype(np.float64),
        },
        attrs={
            "source": "DWD ICON-D2 (opendata.dwd.de)",
            "tier": "tier1",
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bbox": list(ALPEN_BBOX.bounds),
        },
    )


def append_tier1_to_zarr(
    grib_paths: dict[tuple[str, int], Path],
    zarr_path: Path,
    init: dt.datetime,
) -> int:
    """Stack tier-1 GRIBs and append to the daily Zarr archive.

    Returns the number of time-steps written (0 if nothing usable).
    """
    ds = _build_dataset(grib_paths, init)
    if ds is None:
        return 0

    zarr_path.parent.mkdir(parents=True, exist_ok=True)
    encoding = {
        var: {"chunks": (24, ds.sizes["latitude"], ds.sizes["longitude"])}
        for var in ds.data_vars
    }
    if zarr_path.exists():
        ds.to_zarr(zarr_path, mode="a", append_dim="time")
    else:
        ds.to_zarr(zarr_path, mode="w", encoding=encoding)
    return int(ds.sizes["time"])
