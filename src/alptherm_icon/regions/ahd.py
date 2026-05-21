"""Area-Height-Distribution per region (plan §3.2).

Liechti's key parameter:
- S_G(z): DEM pixel area in each 100 m height class, scaled by region area
  (the "Heizfläche" — solar-heated surface available to thermals).
- V_a(z): residual atmospheric volume per 100 m layer = max layer volume
  minus terrain-occupied volume.

Both are deterministic from the DEM + region polygon. Computed once,
stored as NetCDF/pickle, then reused statically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.mask
import xarray as xr
from shapely.geometry.base import BaseGeometry

BIN_HEIGHT_M = 100.0
"""Liechti's standard height-class width."""


@dataclass
class AHDProfile:
    region_name: str
    z_bottom_m: np.ndarray  # bin lower edge (m above sea level)
    z_top_m: np.ndarray  # bin upper edge
    s_g: np.ndarray  # heated terrain surface per bin (m²)
    v_a: np.ndarray  # residual atmospheric volume per bin (m³)
    region_area_m2: float
    mean_slope_deg: np.ndarray | None = None  # plan §3.2 erweiterungs-hook
    mean_aspect_deg: np.ndarray | None = None

    def to_dataset(self) -> xr.Dataset:
        z_center = 0.5 * (self.z_bottom_m + self.z_top_m)
        data_vars = {
            "s_g": ("z", self.s_g, {"long_name": "heated terrain surface", "units": "m2"}),
            "v_a": ("z", self.v_a, {"long_name": "residual atmospheric volume", "units": "m3"}),
            "z_bottom": ("z", self.z_bottom_m, {"units": "m"}),
            "z_top": ("z", self.z_top_m, {"units": "m"}),
        }
        if self.mean_slope_deg is not None:
            data_vars["mean_slope"] = ("z", self.mean_slope_deg, {"units": "deg"})
        if self.mean_aspect_deg is not None:
            data_vars["mean_aspect"] = ("z", self.mean_aspect_deg, {"units": "deg"})
        return xr.Dataset(
            data_vars=data_vars,
            coords={"z": ("z", z_center, {"units": "m"})},
            attrs={
                "region_name": self.region_name,
                "region_area_m2": float(self.region_area_m2),
                "bin_height_m": float(BIN_HEIGHT_M),
            },
        )


def compute_ahd(
    dem_path: str | Path,
    region_geom: BaseGeometry,
    region_name: str,
    bin_height_m: float = BIN_HEIGHT_M,
) -> AHDProfile:
    """Compute the AHD for a region from a DEM raster.

    DEM must be in a projected CRS with metric pixel sizes (e.g. UTM)
    *or* the DEM CRS will be reprojected — for the Alpine pilot we
    assume the caller supplies a UTM-clipped Copernicus GLO-30 tile.
    Region geometry is reprojected to the DEM CRS internally.
    """
    region_geom_4326 = region_geom  # caller responsibility: lon/lat
    with rasterio.open(dem_path) as src:
        from pyproj import Transformer
        from shapely.ops import transform as shp_transform

        if src.crs.to_epsg() != 4326:
            transformer = Transformer.from_crs(4326, src.crs, always_xy=True)
            region_geom_proj = shp_transform(transformer.transform, region_geom_4326)
        else:
            region_geom_proj = region_geom_4326

        masked, masked_transform = rasterio.mask.mask(
            src, [region_geom_proj.__geo_interface__], crop=True, filled=False
        )
        elevations = masked[0]
        pixel_area_m2 = abs(masked_transform.a * masked_transform.e)

    valid = ~np.ma.getmaskarray(elevations)
    elev_valid = np.asarray(elevations[valid], dtype=np.float64)
    if elev_valid.size == 0:
        raise ValueError(f"no DEM pixels inside region {region_name!r}")

    region_area_m2 = float(elev_valid.size * pixel_area_m2)
    z_min = np.floor(elev_valid.min() / bin_height_m) * bin_height_m
    z_max = np.ceil(elev_valid.max() / bin_height_m) * bin_height_m
    edges = np.arange(z_min, z_max + bin_height_m, bin_height_m)
    pixel_counts, _ = np.histogram(elev_valid, bins=edges)

    z_bottom = edges[:-1]
    z_top = edges[1:]
    pixels_in_bin = pixel_counts.astype(np.float64)
    pixels_above_top = float(elev_valid.size) - np.cumsum(pixels_in_bin)
    s_g = pixels_in_bin * pixel_area_m2
    # Terrain occupies the full layer for pixels whose elevation is above the
    # top edge, and on average half the layer for pixels whose elevation falls
    # within the bin (uniform-within-bin assumption).
    occupied_volume = (
        pixel_area_m2 * bin_height_m * (pixels_above_top + 0.5 * pixels_in_bin)
    )
    max_layer_volume = region_area_m2 * bin_height_m
    v_a = max_layer_volume - occupied_volume

    return AHDProfile(
        region_name=region_name,
        z_bottom_m=z_bottom,
        z_top_m=z_top,
        s_g=s_g,
        v_a=v_a,
        region_area_m2=region_area_m2,
    )
