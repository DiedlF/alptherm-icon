"""DEM-Batch und AHD-Batch für das alpenweite Regions-v0 (Plan §3.2).

Sprint 2 zum geometrischen Gerüst aus :mod:`alpine_v0`. Statt pro Region
einen eigenen DEM-Mosaic zu bauen (zu langsam für ~660 Regionen), wird:

1. *Einmal* alle Copernicus-30m-Tiles für den Alpen-Bbox heruntergeladen
   — ~72 Tiles, ~3,6 GB. Tiles über reinem Ozean fehlen schlicht; das
   ist erwartet und kein Fehler.
2. *Einmal* ein alpenweiter DEM-Mosaic in EPSG:3035 gebaut — ~5 GB. Der
   ist die Single Source of Truth für alle nachfolgenden Per-Region-
   Operationen.
3. *Pro Region* aus dem Mosaic maskiert (rasterio.mask) und die
   AHD-Berechnung aus :mod:`ahd` darauf gefahren. Mittlere Höhe fällt
   als Beiprodukt ab und wird als Attribut zurück ans GeoJSON
   geschrieben.

Das DEM bleibt out-of-tree (gitignored, ~5 GB).
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.mask
from rasterio.io import MemoryFile  # noqa: F401  (kept for future in-mem ops)
from rasterio.merge import merge as rio_merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box

from alptherm_icon.regions.ahd import AHDProfile, compute_ahd
from alptherm_icon.regions.alpine_v0 import ALPEN_BBOX
from alptherm_icon.regions.dem import (
    TARGET_CRS,
    TARGET_RES_M,
    TileId,
    download_tile,
    tiles_for_bounds,
)

log = logging.getLogger(__name__)


# Alpine vs. Vorland threshold — plan §3.1.1 says "Mixed-Layer-Modell auf
# Basis derselben ICON-Wärmeströme, ohne Talvolumen-Logik" für Vorland.
# Konkrete Schwelle: mittlere Höhe der Region < 600 m MSL. Liegt zwischen
# Bayerischem Vorland (~500 m) und den niedrigsten Voralpentälern (~700 m).
VORLAND_MAX_ELEV_M = 600.0


@dataclass
class TileDownloadSummary:
    requested: int
    downloaded: int
    cached: int
    missing: int  # ocean tiles, expected
    bytes_total: int


def download_alpine_tiles(
    tiles_dir: Path,
    bbox: tuple[float, float, float, float] = ALPEN_BBOX,
) -> TileDownloadSummary:
    """Download every Copernicus tile intersecting the Alpen-Bbox.

    Idempotent — already-cached tiles are skipped. Ocean tiles (404) are
    counted but not errored.
    """
    tiles = tiles_for_bounds(*bbox)
    cached = 0
    downloaded = 0
    missing = 0
    bytes_total = 0
    for i, tile in enumerate(tiles, 1):
        dest = tiles_dir / f"{tile.basename}.tif"
        if dest.exists() and dest.stat().st_size > 0:
            cached += 1
            bytes_total += dest.stat().st_size
            continue
        try:
            path = download_tile(tile, tiles_dir)
        except FileNotFoundError:
            missing += 1
            log.info("ocean tile (404), skipping: %s", tile.basename)
            continue
        except Exception as exc:  # noqa: BLE001 — keep batching
            log.warning("download failed for %s: %r", tile.basename, exc)
            missing += 1
            continue
        downloaded += 1
        bytes_total += path.stat().st_size
        if downloaded % 5 == 0 or i == len(tiles):
            log.info(
                "tiles: %d/%d done (downloaded=%d cached=%d missing=%d, %.1f GB)",
                i,
                len(tiles),
                downloaded,
                cached,
                missing,
                bytes_total / 1e9,
            )
    return TileDownloadSummary(
        requested=len(tiles),
        downloaded=downloaded,
        cached=cached,
        missing=missing,
        bytes_total=bytes_total,
    )


def build_alpine_mosaic(
    tiles_dir: Path,
    out_path: Path,
    bbox: tuple[float, float, float, float] = ALPEN_BBOX,
    target_crs: str = TARGET_CRS,
    target_res_m: float = TARGET_RES_M,
) -> Path:
    """Reproject + mosaic all Alpen-Bbox tiles into one EPSG:3035 GeoTIFF.

    Idempotent — returns the existing mosaic if already built (delete to
    force rebuild). The mosaic is the single source for the per-region
    operations that follow; building it once is ~10× faster than building
    it per region (~660 regions).

    Uses ``gdalbuildvrt`` + ``gdalwarp`` via subprocess — pure-Python
    ``rasterio.merge`` would materialise the full union in memory (~6 GB
    float32 source + 3 GB target) which exceeds typical HomeServer RAM.
    GDAL's tiled-warp is block-streamed and stays well under 1 GB.
    """
    import subprocess

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    tile_paths: list[Path] = []
    for tile in tiles_for_bounds(*bbox):
        p = tiles_dir / f"{tile.basename}.tif"
        if p.exists() and p.stat().st_size > 0:
            tile_paths.append(p)
    if not tile_paths:
        raise RuntimeError(
            f"no Copernicus tiles found under {tiles_dir} — "
            "run download_alpine_tiles() first"
        )
    log.info("warping %d tiles via gdalwarp → %s", len(tile_paths), out_path.name)

    vrt_path = out_path.with_suffix(".vrt")
    tmp_tif = out_path.with_suffix(".tif.part")
    # Stage 1: virtual mosaic, no actual reprojection / disk write yet.
    cmd_vrt = [
        "gdalbuildvrt",
        "-q",
        "-resolution",
        "highest",
        str(vrt_path),
        *(str(p) for p in tile_paths),
    ]
    subprocess.run(cmd_vrt, check=True)
    # Stage 2: tiled-warp into final EPSG:3035 GeoTIFF, block-streamed.
    cmd_warp = [
        "gdalwarp",
        "-q",
        "-t_srs",
        target_crs,
        "-tr",
        str(target_res_m),
        str(target_res_m),
        "-r",
        "bilinear",
        "-of",
        "GTiff",
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "TILED=YES",
        "-co",
        "BLOCKXSIZE=512",
        "-co",
        "BLOCKYSIZE=512",
        "-co",
        "BIGTIFF=IF_SAFER",
        "-multi",
        "-wo",
        "NUM_THREADS=ALL_CPUS",
        # Cap GDAL's cache so warp stays well under ~1 GB total resident.
        "--config",
        "GDAL_CACHEMAX",
        "512",
        str(vrt_path),
        str(tmp_tif),
    ]
    subprocess.run(cmd_warp, check=True)
    tmp_tif.replace(out_path)
    try:
        vrt_path.unlink()
    except FileNotFoundError:
        pass
    return out_path


@dataclass
class RegionAHDResult:
    region_id: str         # canonical string ID (equals str(hybas_id) for HydroBASINS)
    hybas_id: int          # 0 when region_id is not a numeric HydroBASINS ID
    region_name: str
    mean_elev_m: float
    n_pixels: int
    profile: AHDProfile


def compute_ahd_batch(
    basins: gpd.GeoDataFrame,
    mosaic_path: Path,
    out_dir: Path,
    overwrite: bool = False,
    region_id_col: str = "HYBAS_ID",
) -> list[RegionAHDResult]:
    """Compute AHD for every basin in ``basins`` against the shared mosaic.

    For each row writes ``out_dir/region_<HYBAS_ID>_ahd.nc``. Returns a
    list of :class:`RegionAHDResult` for the caller to fold back into the
    GeoJSON. Mean elevation falls out of the AHD computation cheaply
    (sum of pixel elevations / n_pixels) and is the key new attribute
    for the alpine/vorland classification.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[RegionAHDResult] = []
    with rasterio.open(mosaic_path) as src:
        target_crs = src.crs
        # We project the basin polygons once to mosaic CRS to avoid
        # repeating the transform setup for every row.
        from pyproj import Transformer
        from shapely.ops import transform as shp_transform

        transformer = Transformer.from_crs(4326, target_crs, always_xy=True)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
            for i, row in basins.reset_index(drop=True).iterrows():
                rid_raw = row[region_id_col]
                rid_str = str(rid_raw)
                hid = int(rid_raw) if region_id_col == "HYBAS_ID" else 0
                name = f"region_{rid_str}"
                out = out_dir / f"{name}_ahd.nc"
                if out.exists() and not overwrite:
                    import xarray as _xr

                    with _xr.open_dataset(out) as ds:
                        results.append(
                            RegionAHDResult(
                                region_id=rid_str,
                                hybas_id=hid,
                                region_name=name,
                                mean_elev_m=float(ds.attrs.get("mean_elev_m", float("nan"))),
                                n_pixels=int(ds.attrs.get("n_pixels", 0)),
                                profile=AHDProfile(
                                    region_name=name,
                                    z_bottom_m=ds["z_bottom"].values,
                                    z_top_m=ds["z_top"].values,
                                    s_g=ds["s_g"].values,
                                    v_a=ds["v_a"].values,
                                    region_area_m2=float(ds.attrs["region_area_m2"]),
                                ),
                            )
                        )
                    continue

                geom = row["geometry"]
                geom_proj = shp_transform(transformer.transform, geom)
                try:
                    masked, masked_transform = rasterio.mask.mask(
                        src,
                        [geom_proj.__geo_interface__],
                        crop=True,
                        filled=False,
                    )
                except (ValueError, IndexError) as exc:
                    log.warning("mask failed for HYBAS_ID=%d: %r", hid, exc)
                    continue

                elev = masked[0]
                valid = ~np.ma.getmaskarray(elev)
                elev_valid = np.asarray(elev[valid], dtype=np.float64)
                if elev_valid.size == 0:
                    log.warning("no pixels inside HYBAS_ID=%d", hid)
                    continue
                pixel_area_m2 = abs(masked_transform.a * masked_transform.e)
                mean_elev = float(elev_valid.mean())

                # Re-use the AHD math from ahd.py by writing the masked
                # array out to a temp memory file. Simpler than refactoring
                # compute_ahd() to accept a numpy array.
                bin_height_m = 100.0
                z_min = float(np.floor(elev_valid.min() / bin_height_m) * bin_height_m)
                z_max = float(np.ceil(elev_valid.max() / bin_height_m) * bin_height_m)
                edges = np.arange(z_min, z_max + bin_height_m, bin_height_m)
                pixel_counts, _ = np.histogram(elev_valid, bins=edges)
                z_bottom = edges[:-1]
                z_top = edges[1:]
                pixels_in_bin = pixel_counts.astype(np.float64)
                pixels_above_top = float(elev_valid.size) - np.cumsum(pixels_in_bin)
                s_g = pixels_in_bin * pixel_area_m2
                region_area_m2 = float(elev_valid.size * pixel_area_m2)
                occupied_volume = pixel_area_m2 * bin_height_m * (
                    pixels_above_top + 0.5 * pixels_in_bin
                )
                v_a = region_area_m2 * bin_height_m - occupied_volume

                profile = AHDProfile(
                    region_name=name,
                    z_bottom_m=z_bottom,
                    z_top_m=z_top,
                    s_g=s_g,
                    v_a=v_a,
                    region_area_m2=region_area_m2,
                )
                ds = profile.to_dataset()
                ds.attrs["hybas_id"] = hid
                ds.attrs["mean_elev_m"] = mean_elev
                ds.attrs["n_pixels"] = int(elev_valid.size)
                ds.to_netcdf(out)

                results.append(
                    RegionAHDResult(
                        region_id=rid_str,
                        hybas_id=hid,
                        region_name=name,
                        mean_elev_m=mean_elev,
                        n_pixels=int(elev_valid.size),
                        profile=profile,
                    )
                )
                if (i + 1) % 50 == 0:
                    log.info("AHD batch: %d / %d", i + 1, len(basins))
    return results


def annotate_basins(
    basins: gpd.GeoDataFrame, results: list[RegionAHDResult]
) -> gpd.GeoDataFrame:
    """Fold mean_elev + alpine/vorland-Klassifikation zurück in das GeoDF."""
    by_id = {r.hybas_id: r for r in results}
    basins = basins.copy()
    basins["mean_elev_m"] = [
        by_id[int(h)].mean_elev_m if int(h) in by_id else float("nan")
        for h in basins["HYBAS_ID"]
    ]
    basins["habitat_class"] = [
        "alpine" if e > VORLAND_MAX_ELEV_M else "vorland"
        for e in basins["mean_elev_m"].fillna(0.0)
    ]
    return basins


def annotate_regions(
    regions: gpd.GeoDataFrame,
    results: list[RegionAHDResult],
    id_col: str = "region_id",
    mittelgebirge_min_relief_m: float = 400.0,
) -> gpd.GeoDataFrame:
    """Fold AHD results back into a v2 regions GeoDataFrame.

    Adds mean_elev_m and elev_range_m columns. Refines terrain_type
    for 'non_alpine' rows: once elev_range_m is known, classifies as
    'mittelgebirge' or 'flachland'.
    """
    by_id = {r.region_id: r for r in results}
    regions = regions.copy()
    regions["mean_elev_m"] = [
        by_id[str(rid)].mean_elev_m if str(rid) in by_id else float("nan")
        for rid in regions[id_col]
    ]
    regions["elev_range_m"] = [
        float(
            by_id[str(rid)].profile.z_top_m[-1]
            - by_id[str(rid)].profile.z_bottom_m[0]
        )
        if str(rid) in by_id and len(by_id[str(rid)].profile.z_bottom_m) > 0
        else float("nan")
        for rid in regions[id_col]
    ]
    if "terrain_type" in regions.columns:
        def _refine(row: "pd.Series") -> str:  # type: ignore[name-defined]
            if row["terrain_type"] != "non_alpine":
                return row["terrain_type"]
            r = row.get("elev_range_m", float("nan"))
            if not np.isfinite(r):
                return "non_alpine"
            return "mittelgebirge" if r >= mittelgebirge_min_relief_m else "flachland"
        regions["terrain_type"] = regions.apply(_refine, axis=1)
    return regions
