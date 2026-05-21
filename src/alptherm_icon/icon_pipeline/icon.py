"""DWD ICON-D2 surface-variable fetcher (Komp. B v0.1).

Pulls individual GRIB2 forecast files from the DWD Open Data server
(`opendata.dwd.de`, public HTTPS, no auth), decompresses them, and
extracts a time series at the region centroid and a region-polygon
mean per requested variable.

Scope: surface ("single-level") variables only — vertical model-level
profiles are a follow-up. DWD keeps only the last ~48 h of init runs,
so we mirror to `data/icon/` for any offline / retrospective use
(plan §4.5).

URL pattern (verified empirically on opendata.dwd.de):
    {base}/{HH}/{var}/icon-d2_germany_regular-lat-lon_single-level_
        {YYYYMMDDHH}_{lead:03d}_2d_{var}.grib2.bz2
"""

from __future__ import annotations

import bz2
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import rasterio.features
import requests
import xarray as xr
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

ICON_D2_BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-d2/grib"
ICON_D2_GRID = "regular-lat-lon"
ICON_D2_INIT_HOURS = (0, 3, 6, 9, 12, 15, 18, 21)
ICON_D2_MAX_LEAD = 48
ICON_D2_N_FULL_LEVELS = 65
"""Number of native model levels in ICON-D2 (full levels). HHL has N+1=66 half-levels."""

VAR_ATTRS = {
    "t": {"units": "K", "long_name": "air temperature"},
    "qv": {"units": "kg/kg", "long_name": "specific humidity"},
    "relhum": {"units": "%", "long_name": "relative humidity"},
    "u": {"units": "m/s", "long_name": "zonal wind"},
    "v": {"units": "m/s", "long_name": "meridional wind"},
    "p": {"units": "Pa", "long_name": "pressure"},
}


VALID_LEVEL_TYPES = ("single-level", "model-level", "time-invariant")


@dataclass(frozen=True)
class IconD2File:
    """One ICON-D2 GRIB2 file (single init, single lead, single variable, single level).

    Three level-type variants:

    - ``single-level``: surface fields (e.g. t_2m, asob_s). ``level`` must be
      None. Filename gets a ``2d_<var>`` trailing token.
    - ``model-level``: native 3D model levels (1..65 in ICON-D2, with 65 being
      closest to the surface). ``level`` required. Trailing token is
      ``<level>_<var>``, level **not** zero-padded per DWD convention.
    - ``time-invariant``: HHL half-level heights, published only with the
      00 UTC init. Same trailing-token rule as model-level.
    """

    init: datetime
    lead_h: int
    var: str  # lowercase DWD token: t_2m, asob_s, t, qv, hhl, ...
    level_type: str = "single-level"
    level: int | None = None

    def __post_init__(self) -> None:
        if self.init.tzinfo not in (None, timezone.utc):
            raise ValueError(f"init must be UTC or naive, got {self.init.tzinfo}")
        if self.init.hour not in ICON_D2_INIT_HOURS:
            raise ValueError(
                f"ICON-D2 init hour must be one of {ICON_D2_INIT_HOURS}, "
                f"got {self.init.hour:02d}"
            )
        if not (0 <= self.lead_h <= ICON_D2_MAX_LEAD):
            raise ValueError(
                f"lead_h must be in [0, {ICON_D2_MAX_LEAD}], got {self.lead_h}"
            )
        if self.level_type not in VALID_LEVEL_TYPES:
            raise ValueError(
                f"level_type must be one of {VALID_LEVEL_TYPES}, got {self.level_type!r}"
            )
        if self.level_type == "single-level" and self.level is not None:
            raise ValueError("level must be None for single-level files")
        if self.level_type != "single-level" and self.level is None:
            raise ValueError(f"level required for {self.level_type} files")

    @property
    def init_str(self) -> str:
        return self.init.strftime("%Y%m%d%H")

    @property
    def filename_bz2(self) -> str:
        if self.level_type == "single-level":
            var_token = f"2d_{self.var}"
        else:
            # model-level + time-invariant: level NOT zero-padded (DWD convention).
            var_token = f"{self.level}_{self.var}"
        return (
            f"icon-d2_germany_{ICON_D2_GRID}_{self.level_type}_"
            f"{self.init_str}_{self.lead_h:03d}_{var_token}.grib2.bz2"
        )

    @property
    def filename_grib2(self) -> str:
        return self.filename_bz2.removesuffix(".bz2")

    @property
    def url(self) -> str:
        hh = f"{self.init.hour:02d}"
        return f"{ICON_D2_BASE_URL}/{hh}/{self.var}/{self.filename_bz2}"


def download_and_decompress(
    spec: IconD2File,
    cache_dir: Path,
    timeout_s: float = 120.0,
) -> Path | None:
    """Download a single GRIB2 file, decompress, cache. Idempotent.

    Returns the decompressed .grib2 path, or None if the upstream file is gone
    (HTTP 404 — common once a run rolls out of the 2-day window).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    grib_path = cache_dir / spec.filename_grib2
    if grib_path.exists() and grib_path.stat().st_size > 0:
        return grib_path

    tmp_bz2 = cache_dir / (spec.filename_bz2 + ".part")
    with requests.get(spec.url, stream=True, timeout=timeout_s) as resp:
        if resp.status_code == 404:
            log.warning("ICON-D2 file not on DWD server: %s", spec.url)
            return None
        resp.raise_for_status()
        with tmp_bz2.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    # Stream-decompress without keeping the .bz2 around.
    tmp_grib = grib_path.with_suffix(".grib2.part")
    with bz2.open(tmp_bz2, "rb") as src, tmp_grib.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp_bz2.unlink()
    tmp_grib.replace(grib_path)
    return grib_path


def _lat_lon_names(ds: xr.Dataset) -> tuple[str, str]:
    """Return the latitude/longitude coord names for a cfgrib-opened dataset."""
    lat_candidates = [n for n in ("latitude", "lat", "y") if n in ds.coords]
    lon_candidates = [n for n in ("longitude", "lon", "x") if n in ds.coords]
    if not lat_candidates or not lon_candidates:
        raise KeyError(f"no lat/lon coords on dataset (have {list(ds.coords)})")
    return lat_candidates[0], lon_candidates[0]


def extract_at_region(
    ds: xr.Dataset,
    region_geom: BaseGeometry,
    var_name: str,
) -> tuple[xr.DataArray, xr.DataArray]:
    """Return (centroid_value, region_mean) — both 0-D for a single time slice."""
    lat_name, lon_name = _lat_lon_names(ds)
    centroid = region_geom.representative_point()
    da = ds[var_name]
    centroid_val = da.sel({lat_name: centroid.y, lon_name: centroid.x}, method="nearest")

    # Polygon mask on the dataset grid via rasterio.features.geometry_mask.
    lats = ds[lat_name].values
    lons = ds[lon_name].values
    dlat = float(np.abs(np.diff(lats)).mean())
    dlon = float(np.abs(np.diff(lons)).mean())
    # rasterio uses an (origin_x, pixel_w, 0, origin_y, 0, pixel_h) affine, with
    # origin at the upper-left and pixel_h negative. Match the orientation of
    # the dataset axes (DWD lat-lon files are typically lat-descending).
    lat_desc = lats[0] > lats[-1]
    origin_y = lats[0] + (dlat / 2 if lat_desc else -dlat / 2)
    pixel_h = -dlat if lat_desc else dlat
    origin_x = lons[0] - dlon / 2
    from rasterio.transform import Affine

    transform = Affine(dlon, 0.0, origin_x, 0.0, pixel_h, origin_y)
    mask = rasterio.features.geometry_mask(
        [region_geom.__geo_interface__],
        out_shape=(lats.size, lons.size),
        transform=transform,
        invert=True,  # True where inside polygon
        all_touched=False,
    )
    if not mask.any():
        raise ValueError(
            f"no ICON grid cells inside region polygon (bounds {region_geom.bounds})"
        )
    region_mean = da.where(xr.DataArray(mask, dims=(lat_name, lon_name))).mean(
        dim=(lat_name, lon_name)
    )
    return centroid_val, region_mean


def fetch_surface_series(
    region_geom: BaseGeometry,
    region_name: str,
    init: datetime,
    variables: Iterable[str],
    cache_dir: Path,
    lead_max: int = ICON_D2_MAX_LEAD,
) -> xr.Dataset:
    """Fetch all (var, lead) GRIB2s, extract centroid + region mean per var.

    Returns a Dataset with a `time` dim of (lead_max+1,) entries and two
    DataArrays per variable: `<var>_centroid` and `<var>_mean`.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    series: dict[str, list[float]] = {}
    times: list[np.datetime64] = []

    for lead in range(0, lead_max + 1):
        valid_time = np.datetime64(init.replace(tzinfo=None)) + np.timedelta64(lead, "h")
        times.append(valid_time)
        for var in variables:
            spec = IconD2File(init=init, lead_h=lead, var=var)
            grib_path = download_and_decompress(spec, cache_dir)
            if grib_path is None:
                series.setdefault(f"{var}_centroid", []).append(np.nan)
                series.setdefault(f"{var}_mean", []).append(np.nan)
                continue
            ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
            data_var = [v for v in ds.data_vars][0]  # ICON-D2 GRIB2 has one var per file
            c_val, m_val = extract_at_region(ds, region_geom, data_var)
            series.setdefault(f"{var}_centroid", []).append(float(c_val.values))
            series.setdefault(f"{var}_mean", []).append(float(m_val.values))
            ds.close()

    return xr.Dataset(
        data_vars={k: ("time", np.asarray(v, dtype=np.float64)) for k, v in series.items()},
        coords={"time": np.asarray(times, dtype="datetime64[ns]")},
        attrs={
            "region_name": region_name,
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "DWD ICON-D2 (opendata.dwd.de)",
            "grid": ICON_D2_GRID,
        },
    )


def _extract_centroid_only(grib_path: Path, region_geom: BaseGeometry) -> float:
    """Open a GRIB2 file, take its single data var, return centroid value."""
    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        data_var = next(iter(ds.data_vars))
        c_val, _ = extract_at_region(ds, region_geom, data_var)
        return float(c_val.values)
    finally:
        ds.close()


def fetch_profile(
    region_geom: BaseGeometry,
    region_name: str,
    init: datetime,
    lead_h: int,
    cache_dir: Path,
    variables: Iterable[str] = ("t", "qv"),
    n_levels: int = ICON_D2_N_FULL_LEVELS,
    include_heights: bool = True,
) -> xr.Dataset:
    """Fetch a 1D vertical profile at the region centroid for one (init, lead).

    For each variable, pulls all `n_levels` model-level GRIB2 files, opens
    them with cfgrib, and extracts the value at the centroid. Heights come
    from HHL (time-invariant half-level heights), fetched from the 00 UTC
    init of the same calendar day — full-level heights are computed as
    midpoints between consecutive half-levels.

    Returns a Dataset with `level` dim (1..n_levels, where 1 is the model
    top and `n_levels` is closest to the surface in ICON-D2's convention)
    and one DataArray per requested variable plus optional `height`.

    Note: HHL is only published with the 00 UTC init. If `init` is not at
    00 UTC, this still works (HHL is geometry, not time-dependent) but
    requires the same calendar day's 00 run to have been published.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    levels = np.arange(1, n_levels + 1, dtype=np.int64)

    var_values: dict[str, np.ndarray] = {}
    for var in variables:
        arr = np.full(n_levels, np.nan, dtype=np.float64)
        for k in range(1, n_levels + 1):
            spec = IconD2File(
                init=init, lead_h=lead_h, var=var, level_type="model-level", level=k
            )
            grib = download_and_decompress(spec, cache_dir)
            if grib is None:
                continue
            arr[k - 1] = _extract_centroid_only(grib, region_geom)
        var_values[var] = arr

    heights = None
    if include_heights:
        hhl_init = init.replace(hour=0)
        n_half = n_levels + 1
        half_heights = np.full(n_half, np.nan, dtype=np.float64)
        for k in range(1, n_half + 1):
            spec = IconD2File(
                init=hhl_init,
                lead_h=0,
                var="hhl",
                level_type="time-invariant",
                level=k,
            )
            grib = download_and_decompress(spec, cache_dir)
            if grib is None:
                continue
            half_heights[k - 1] = _extract_centroid_only(grib, region_geom)
        heights = 0.5 * (half_heights[:-1] + half_heights[1:])

    data_vars: dict[str, tuple] = {}
    for var, arr in var_values.items():
        attrs = VAR_ATTRS.get(var, {"long_name": var})
        data_vars[var] = (("level",), arr, attrs)
    if heights is not None:
        data_vars["height"] = (
            ("level",),
            heights,
            {"units": "m", "long_name": "geometric height of full model level"},
        )

    valid_time = init + timedelta(hours=lead_h)
    return xr.Dataset(
        data_vars=data_vars,
        coords={"level": ("level", levels, {"long_name": "ICON-D2 model level (1=top, N=surface)"})},
        attrs={
            "region_name": region_name,
            "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lead_h": int(lead_h),
            "valid_time_utc": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "DWD ICON-D2 model-level + HHL (opendata.dwd.de)",
            "grid": ICON_D2_GRID,
            "n_levels": int(n_levels),
        },
    )
