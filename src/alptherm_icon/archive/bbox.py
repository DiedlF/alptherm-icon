"""Alpen-Bbox + xarray subset helper for the M0 archive.

The DWD ICON-D2 public product is the *Germany* lat-lon crop (roughly
0.05° resolution, ~3.5°E..17.5°E, 43.2°N..58.1°N). The Alpine subset we
care about for ALPTHERM lives in the southern half of that grid. We
keep the raw GRIB2 files unmodified (re-encoding GRIB without CDO is
brittle), but when we lift Tier-1 surface fields into the daily Zarr
archive we crop to ALPEN_BBOX to roughly halve on-disk size and make
later reads of "the Alps" trivial.
"""

from __future__ import annotations

from dataclasses import dataclass

import xarray as xr


@dataclass(frozen=True)
class BBox:
    """Inclusive lat/lon bounding box in EPSG:4326."""

    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    def __post_init__(self) -> None:
        if self.lon_min >= self.lon_max:
            raise ValueError(f"lon_min {self.lon_min} >= lon_max {self.lon_max}")
        if self.lat_min >= self.lat_max:
            raise ValueError(f"lat_min {self.lat_min} >= lat_max {self.lat_max}")

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)


# Alps domain that intersects the ICON-D2 Germany product.
# Western edge picks up the Französische / Westschweizer Alpen; eastern
# edge runs into the Niedere Tauern / Wienerwald. Northern edge sits in
# the Bavarian Vorland (deliberately generous — Komp. A Vorland-Klasse).
ALPEN_BBOX = BBox(lon_min=5.0, lat_min=43.5, lon_max=17.0, lat_max=49.0)


def _lat_lon_names(ds: xr.Dataset) -> tuple[str, str]:
    lat = next((n for n in ("latitude", "lat", "y") if n in ds.coords), None)
    lon = next((n for n in ("longitude", "lon", "x") if n in ds.coords), None)
    if lat is None or lon is None:
        raise KeyError(f"no lat/lon coords on dataset (have {list(ds.coords)})")
    return lat, lon


def subset_to_bbox(ds: xr.Dataset, bbox: BBox = ALPEN_BBOX) -> xr.Dataset:
    """Slice an xarray dataset to ``bbox``. Handles lat-descending grids."""
    lat_name, lon_name = _lat_lon_names(ds)
    lats = ds[lat_name].values
    lat_desc = lats[0] > lats[-1]
    lat_slice = (
        slice(bbox.lat_max, bbox.lat_min) if lat_desc else slice(bbox.lat_min, bbox.lat_max)
    )
    return ds.sel({lat_name: lat_slice, lon_name: slice(bbox.lon_min, bbox.lon_max)})
