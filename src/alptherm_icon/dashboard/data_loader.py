"""Pure read-side data access for the dashboard (Plan §10.1).

Returns plain dataclasses / dicts — no Streamlit imports here, so the
loaders can be unit-tested without spinning up the runtime. The
dashboard pages decorate calls with ``@st.cache_data(ttl=...)``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alptherm_icon import monitoring
from alptherm_icon.archive import manifest as manifest_mod
from alptherm_icon.archive.archiver import ArchiveRoot
from alptherm_icon.monitoring.alerter import Alert, AlerterConfig, check
from alptherm_icon.monitoring.heartbeat import HeartbeatStatus
from alptherm_icon.ogn.writer import raw_log_path


def project_root() -> Path:
    """Walk up from CWD looking for pyproject.toml. The dashboard runs
    out of the same project layout as the archive/OGN code."""
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"no pyproject.toml at or above {here}")


# ---------------------------------------------------------------------------
# Fast gzip-stream helpers (pigz-accelerated, used by the OGN loaders)
# ---------------------------------------------------------------------------


def _open_decompressed(path: Path):  # type: ignore[no-untyped-def]
    """Context manager yielding a binary stream of decompressed bytes.

    Prefers ``pigz -dc`` over stdlib ``gzip.open`` — gzip-decode is
    single-threaded in ``gzip``, while ``pigz`` parallelises the
    chunk-decompression across cores. On the live-written OGN log this
    cuts pure decompression time from ~12 s to ~3 s for a 1 GB file
    (4-core box). Falls back transparently when pigz isn't installed.
    """
    import contextlib
    import gzip
    import shutil
    import subprocess

    @contextlib.contextmanager
    def _ctx():  # type: ignore[no-untyped-def]
        pigz = shutil.which("pigz")
        if pigz is None:
            fh = gzip.open(path, "rb")
            try:
                yield fh
            finally:
                fh.close()
            return
        proc = subprocess.Popen(
            [pigz, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield proc.stdout
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    return _ctx()


def _iter_chunks(path: Path, chunk_size: int = 8 << 20):
    """Stream the decompressed file in fixed-size byte chunks.

    EOFError (live-tail) is swallowed — what we've already produced is
    handed to the caller.
    """
    try:
        with _open_decompressed(path) as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    except EOFError:
        return


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def load_heartbeats(root: Path) -> list[HeartbeatStatus]:
    """All heartbeats currently on disk, sorted by job name."""
    return monitoring.read_all(root)


@dataclass
class AlertSummary:
    alerts: list[Alert] = field(default_factory=list)

    @property
    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.alerts:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out


def load_alerts(root: Path) -> AlertSummary:
    """Run the alerter once and return its current view."""
    config = AlerterConfig()  # default thresholds, no webhook delivery
    return AlertSummary(alerts=check(root, config))


# ---------------------------------------------------------------------------
# Manifest summary
# ---------------------------------------------------------------------------


@dataclass
class ManifestSummary:
    rows: list[dict[str, Any]] = field(default_factory=list)
    by_tier: dict[str, int] = field(default_factory=dict)
    fired_decisions: int = 0
    pending_downloads: int = 0
    total_bytes: int = 0

    @property
    def has_data(self) -> bool:
        return bool(self.rows)


def load_manifest_summary(root: Path) -> ManifestSummary:
    paths = ArchiveRoot(root=root)
    rows = manifest_mod.read_all(paths.manifest_path)
    if not rows:
        return ManifestSummary()
    by_tier: dict[str, int] = {}
    fired = 0
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        if r["tier"] == "tier2_decision" and (r.get("trigger") or {}).get("fire"):
            fired += 1
    pending = manifest_mod.pending_tier2_targets(paths.manifest_path)
    total_bytes = sum(r.get("bytes_on_disk", 0) for r in rows)
    return ManifestSummary(
        rows=rows,
        by_tier=by_tier,
        fired_decisions=fired,
        pending_downloads=len(pending),
        total_bytes=total_bytes,
    )


# ---------------------------------------------------------------------------
# Storage stats
# ---------------------------------------------------------------------------


@dataclass
class StorageStats:
    archive_bytes: int = 0
    ogn_bytes: int = 0
    zarr_bytes: int = 0
    grib_bytes: int = 0
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0

    @property
    def total_managed_bytes(self) -> int:
        return self.archive_bytes + self.ogn_bytes


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def load_storage(root: Path) -> StorageStats:
    import shutil

    archive_dir = root / "data" / "archive"
    ogn_dir = root / "data" / "ogn"
    grib_dir = archive_dir / "grib"
    zarr_dir = archive_dir / "zarr"
    archive_bytes = _dir_size(archive_dir)
    ogn_bytes = _dir_size(ogn_dir)
    grib_bytes = _dir_size(grib_dir)
    zarr_bytes = _dir_size(zarr_dir)

    try:
        usage = shutil.disk_usage(str(root))
        disk_total = usage.total
        disk_free = usage.free
    except OSError:
        disk_total = disk_free = 0

    return StorageStats(
        archive_bytes=archive_bytes,
        ogn_bytes=ogn_bytes,
        zarr_bytes=zarr_bytes,
        grib_bytes=grib_bytes,
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
    )


# ---------------------------------------------------------------------------
# OGN inventory
# ---------------------------------------------------------------------------


def _safe_simplify(gdf, tol: float):
    """Simplify each geometry defensively.

    The vectorised geopandas/shapely path occasionally trips on a
    pathological coordinate at tolerances like 0.005° and raises
    ``IllegalArgumentException: CGAlgorithmsDD::orientationIndex
    encountered NaN/Inf numbers`` — the whole call dies even though
    only one feature is bad. Per-row simplify with ``make_valid`` and
    a try/except keeps the rest of the layer working. ~833 rows of
    per-row simplify is < 50 ms on this dataset, no perceivable hit.
    """
    import shapely.validation as _v

    def _one(g):
        if g is None or g.is_empty:
            return None
        try:
            return g.simplify(tol).buffer(0)
        except Exception:  # noqa: BLE001 — GEOS edge case on this feature
            pass
        # second attempt: make_valid + retry
        try:
            return _v.make_valid(g).simplify(tol).buffer(0)
        except Exception:  # noqa: BLE001
            return None

    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.apply(_one)
    gdf = gdf[gdf.geometry.notna()]
    gdf = gdf[~gdf.geometry.is_empty].reset_index(drop=True)
    return gdf


def _greedy_region_colors(gdf) -> list[int]:
    """Assign a colour index per region so no two adjacent regions share
    one (greedy graph colouring on intersection-adjacency). Planar-ish
    geometries need ≤ ~7 colours. ~0.1 s for 833 regions.
    """
    geoms = list(gdf.geometry)
    sindex = gdf.sindex
    adj: dict[int, set[int]] = {i: set() for i in range(len(geoms))}
    for i, g in enumerate(geoms):
        for j in sindex.query(g):
            if j > i and g.intersects(geoms[j]):
                adj[i].add(int(j))
                adj[int(j)].add(i)
    color_idx = [-1] * len(geoms)
    # Colour higher-degree nodes first — fewer total colours.
    order = sorted(range(len(geoms)), key=lambda i: -len(adj[i]))
    for i in order:
        used = {color_idx[j] for j in adj[i] if color_idx[j] >= 0}
        c = 0
        while c in used:
            c += 1
        color_idx[i] = c
    return color_idx


def load_regions_v2(
    root: Path,
    simplify_deg: float = 0.003,
    with_colors: bool = True,
):  # -> geopandas.GeoDataFrame | None
    """Load v2 SOIUSA-based regions. Prefers the annotated (post-AHD) file,
    falls back to the unannotated build output. Returns None if neither exists.
    """
    import geopandas as gpd

    for candidate in (
        root / "data" / "regions" / "alpine_v2_regions_annotated.geojson",
        root / "data" / "regions" / "alpine_v2_regions.geojson",
    ):
        if candidate.exists():
            gdf = gpd.read_file(candidate)
            if simplify_deg > 0:
                gdf = _safe_simplify(gdf, simplify_deg)
            if with_colors:
                gdf["color_idx"] = _greedy_region_colors(gdf)
            return gdf
    return None


def load_hydrobasins_for_display(
    root: Path,
    level: int = 8,
    simplify_deg: float = 0.004,
):  # -> geopandas.GeoDataFrame | None
    """Load HydroBASINS L{level} clipped to the model domain for map display.

    Reads the shapefile from ``data/basins/`` (downloaded by
    ``python -m alptherm_icon.regions alpine-v2`` or ``fetch_hydrobasins``).
    Returns None if the shapefile hasn't been fetched yet.
    """
    import geopandas as gpd
    from shapely.geometry import box

    from alptherm_icon.regions.alpine_v0 import MODEL_BBOX

    shp = root / "data" / "basins" / f"hybas_eu_lev{level:02d}_v1c.shp"
    if not shp.exists():
        return None
    gdf = gpd.read_file(shp)
    bbox_geom = box(*MODEL_BBOX)
    with __import__("warnings").catch_warnings():
        __import__("warnings").filterwarnings("ignore", message="Geometry is in a geographic CRS")
        candidates = gdf[gdf.intersects(bbox_geom)].copy()
        overlap = candidates.geometry.intersection(bbox_geom).area / candidates.geometry.area
        candidates["overlap_frac"] = overlap.values
        gdf = candidates[candidates["overlap_frac"] >= 0.3].copy()
    if "SUB_AREA" in gdf.columns:
        gdf = gdf.rename(columns={"SUB_AREA": "area_km2"})
    if simplify_deg > 0:
        gdf = _safe_simplify(gdf, simplify_deg)
    return gdf.reset_index(drop=True)


def load_soiusa_groups(
    root: Path,
    simplify_deg: float = 0.004,
):  # -> geopandas.GeoDataFrame | None
    """Load the SOIUSA source group polygons (plan §3.1 Stufe 1).

    These are the orographic *should-be* structure from OSM / local file,
    distinct from the HydroBASINS-realised regions in load_regions_v2.
    Returns None if soiusa_groups.geojson hasn't been built yet.
    """
    import geopandas as gpd

    path = root / "data" / "regions" / "soiusa_groups.geojson"
    if not path.exists():
        return None
    gdf = gpd.read_file(path)
    if simplify_deg > 0:
        gdf = _safe_simplify(gdf, simplify_deg)
    return gdf


def load_domain_boundary(root: Path):  # -> dict | None
    """Load the outer model domain boundary GeoJSON dict, or None."""
    import json

    path = root / "data" / "regions" / "domain_boundary.geojson"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_regions(
    root: Path,
    simplify_deg: float = 0.003,
    with_colors: bool = True,
):  # -> geopandas.GeoDataFrame | None
    """Load the Regions-v1 GeoJSON for display, simplified for the browser.

    833 full-resolution polygons (~10 MB) are sluggish as folium vector
    paths. A ~0.003° (~250 m) Douglas-Peucker simplify cuts the vertex
    count by ~5× with no visible difference at alpine-overview zoom.

    When ``with_colors`` is set, a ``color_idx`` column is added via
    greedy graph colouring so the "einzeln" display scheme can give
    adjacent regions distinct colours. Returns ``None`` if the v1
    GeoJSON hasn't been built yet.
    """
    import geopandas as gpd

    path = root / "data" / "regions" / "alpine_v1_basins.geojson"
    if not path.exists():
        return None
    gdf = gpd.read_file(path)
    if simplify_deg > 0:
        gdf = _safe_simplify(gdf, simplify_deg)
    if with_colors:
        gdf["color_idx"] = _greedy_region_colors(gdf)
    return gdf


def list_thermal_days(root: Path) -> list[str]:
    """Available thermal-parquet days (``YYYY-MM-DD``), newest last."""
    d = root / "data" / "thermals"
    if not d.exists():
        return []
    days = []
    for p in d.glob("*_thermals.parquet"):
        stem = p.name.removesuffix("_thermals.parquet")
        days.append(stem)
    return sorted(days)


def load_thermals(root: Path, day: str | None = None):
    """Load one day's detected thermals (parquet). Newest day if ``day``
    is None. Returns a DataFrame or ``None`` if nothing available."""
    import pandas as pd

    days = list_thermal_days(root)
    if not days:
        return None
    chosen = day if (day in days) else days[-1]
    path = root / "data" / "thermals" / f"{chosen}_thermals.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def load_alpine_perimeter(root: Path, simplify_deg: float = 0.004):
    """Dissolved outer boundary of all alpine regions — our topographic
    Alpenraum-Perimeter (the official Alpine-Convention shapefile isn't
    reachably published; this is the equivalent from our own 600 m
    mean-elevation classification, Plan §3.1.1).

    Returns a shapely geometry (the dissolved alpine area) or ``None``.
    The caller draws its boundary as a line.

    Performance: the actual dissolve takes ~3 s (union of 374 alpine
    polygons). We cache the result as WKB next to the regions GeoJSON
    and invalidate by mtime — subsequent calls load in < 5 ms.
    """
    import shapely.wkb

    regions_path = root / "data" / "regions" / "alpine_v1_basins.geojson"
    cache = root / "data" / "regions" / "alpine_v0_perimeter.wkb"
    if (
        cache.exists()
        and regions_path.exists()
        and cache.stat().st_mtime >= regions_path.stat().st_mtime
    ):
        return shapely.wkb.loads(cache.read_bytes())

    gdf = load_regions(root, simplify_deg=0.002, with_colors=False)
    if gdf is None:
        return None
    alpine = gdf[gdf["habitat_class"] == "alpine"]
    if alpine.empty:
        return None
    dissolved = alpine.geometry.union_all()
    if simplify_deg > 0:
        dissolved = dissolved.simplify(simplify_deg).buffer(0)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(shapely.wkb.dumps(dissolved))
    except OSError:
        pass  # caching is best-effort — never fail the load on cache write
    return dissolved


@dataclass
class OgnDayStats:
    day: dt.date
    bytes_on_disk: int
    path: Path


def load_ogn_inventory(root: Path) -> list[OgnDayStats]:
    """Per-day file sizes for the raw OGN log, sorted by date ascending."""
    raw_dir = root / "data" / "ogn" / "raw"
    if not raw_dir.exists():
        return []
    out: list[OgnDayStats] = []
    for path in raw_dir.rglob("*.jsonl.gz"):
        try:
            # filename is YYYY-MM-DD.jsonl.gz
            day = dt.date.fromisoformat(path.stem.removesuffix(".jsonl"))
            out.append(
                OgnDayStats(day=day, bytes_on_disk=path.stat().st_size, path=path)
            )
        except (ValueError, OSError):
            continue
    return sorted(out, key=lambda s: s.day)


# ---------------------------------------------------------------------------
# Ebene 3 — Inhaltliche Auswertung (Plan §10.2 Ebene 3)
# ---------------------------------------------------------------------------


def load_zarr_timeseries(
    root: Path,
    variables: tuple[str, ...] = ("cape_ml", "asob_s", "htop_dc", "tot_prec"),
    days_back: int | None = 3,
) -> "pandas.DataFrame":  # type: ignore[name-defined]  # noqa: F821
    """Load spatial-max per timestep for the given Zarr variables.

    Returns a long-format DataFrame with columns ``[time, variable, value]``.
    Only the most recent ``days_back`` days of data are returned (default
    3) — keeps the chart readable when the archive grows.
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    zarr_path = root / "data" / "archive" / "zarr" / "tier1.zarr"
    if not zarr_path.exists():
        return pd.DataFrame(columns=["time", "variable", "value"])

    # NB: ``zarr_append`` does not deduplicate when overlapping forecast
    # horizons of different inits arrive (25.5.03Z + lead 3 and 25.5.06Z
    # + lead 0 share the validity time 25.5.06:00). We drop duplicates
    # here at query time. Komp. B M2 should handle this properly via a
    # "most-recent-issued wins" merge — for now the dashboard tolerates
    # it. See plan §4.2 for the pipeline-side fix.
    ds = xr.open_zarr(zarr_path)
    times_raw = pd.to_datetime(ds.time.values)
    # First-occurrence wins; sort to get a monotonic axis afterwards.
    keep_idx = ~times_raw.duplicated(keep="first")
    ds = ds.isel(time=np.where(keep_idx)[0])
    sort_idx = np.argsort(ds.time.values)
    ds = ds.isel(time=sort_idx)

    if days_back is not None:
        latest = pd.Timestamp(ds.time.values[-1])
        cutoff = latest - pd.Timedelta(days=days_back)
        mask = ds.time.values >= np.datetime64(cutoff)
        ds = ds.isel(time=np.where(mask)[0])

    rows: list[dict[str, object]] = []
    for var in variables:
        if var not in ds.data_vars:
            continue
        arr = np.asarray(ds[var].values, dtype=np.float64)
        # Spatial max over (lat, lon) per timestep, ignoring NaN.
        spatial_max = np.nanmax(arr.reshape(arr.shape[0], -1), axis=1)
        for t, v in zip(ds.time.values, spatial_max):
            if np.isfinite(v):
                rows.append({"time": pd.Timestamp(t), "variable": var, "value": float(v)})
    return pd.DataFrame(rows)


@dataclass
class WatchlistEntry:
    """One row in the user-maintained ``data/watchlist.json``."""

    name: str  # human-readable display name
    ogn_name: str  # APRS sender ID, e.g. "FLRDDDD24" or "ICA3F5AB7"
    note: str = ""


@dataclass
class WatchlistPosition:
    """Latest known position for one watchlist entry on a given day."""

    entry: WatchlistEntry
    last_seen_utc: dt.datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    climb_rate: float | None = None
    ground_speed_kmh: float | None = None
    track: float | None = None  # degrees
    aircraft_type: int | None = None
    packets_today: int = 0


def load_watchlist(root: Path) -> list[WatchlistEntry]:
    """Read ``data/watchlist.json`` if it exists; return empty list otherwise."""
    import json

    path = root / "data" / "watchlist.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[WatchlistEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "name" not in item or "ogn_name" not in item:
            continue
        out.append(
            WatchlistEntry(
                name=str(item["name"]),
                ogn_name=str(item["ogn_name"]),
                note=str(item.get("note", "")),
            )
        )
    return out


def load_watchlist_positions(
    root: Path,
    day: dt.date | None = None,
) -> list[WatchlistPosition]:
    """Latest position per watchlist entry from the given day's raw log.

    Strategy: substring-prefilter on the raw APRS line (cheap), parse
    only the hits with ``ogn-parser``. With ~25 M packets/day and a
    typical club watchlist of <20 aircraft, the full pass takes a few
    seconds — acceptable behind a 60 s cache.
    """
    import gzip
    import json as _json

    from ogn.parser import parse as ogn_parse
    from ogn.parser.exceptions import AprsParseError

    watchlist = load_watchlist(root)
    if not watchlist:
        return []

    name_to_position: dict[str, WatchlistPosition] = {
        e.ogn_name: WatchlistPosition(entry=e) for e in watchlist
    }
    interesting_bytes = [n.encode("ascii") for n in name_to_position.keys()]

    inventory = load_ogn_inventory(root)
    if not inventory:
        return list(name_to_position.values())
    stats = (
        next((s for s in inventory if s.day == day), None)
        if day is not None
        else inventory[-1]
    )
    if stats is None:
        return list(name_to_position.values())

    # Performance plan: decompress through pigz (multi-core), scan
    # chunked bytes, substring-prefilter on byte chunks before doing
    # any json.loads / ogn-parser work. ~2–3× faster than gzip+line-iter
    # on a busy OGN day.

    leftover = b""
    for chunk in _iter_chunks(stats.path):
        data = leftover + chunk
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            leftover = data
            continue
        scan, leftover = data[: last_nl + 1], data[last_nl + 1 :]
        # Skip the chunk entirely if no watchlist name is anywhere in
        # these 8 MiB — common case on a busy day where we hit < 0.1 %
        # of lines.
        if not any(s in scan for s in interesting_bytes):
            continue
        for line_bytes in scan.split(b"\n"):
            if not line_bytes or not any(s in line_bytes for s in interesting_bytes):
                continue
            try:
                rec = _json.loads(line_bytes)
            except _json.JSONDecodeError:
                continue
            raw = rec.get("raw", "")
            try:
                parsed = ogn_parse(raw)
            except (AprsParseError, ValueError):
                continue
            if not parsed:
                continue
            name = parsed.get("name")
            if name not in name_to_position:
                continue
            slot = name_to_position[name]
            slot.packets_today += 1
            if parsed.get("aprs_type") != "position":
                continue
            ts_recv_raw = rec.get("ts_recv", "")
            try:
                ts_recv = dt.datetime.strptime(
                    ts_recv_raw[:19] + "Z", "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                ts_recv = None
            slot.last_seen_utc = ts_recv
            slot.latitude = parsed.get("latitude")
            slot.longitude = parsed.get("longitude")
            slot.altitude_m = parsed.get("altitude")
            slot.climb_rate = parsed.get("climb_rate")
            slot.ground_speed_kmh = parsed.get("ground_speed")
            slot.track = parsed.get("track")
            slot.aircraft_type = parsed.get("aircraft_type")

    return list(name_to_position.values())


def load_ogn_hourly_activity(
    root: Path,
    day: dt.date | None = None,
) -> "pandas.DataFrame":  # type: ignore[name-defined]  # noqa: F821
    """Count distinct aircraft IDs per UTC hour for one day's raw log.

    Falls back to the most recent day's file if ``day`` is None.
    Returns columns ``[hour, n_aircraft, n_packets]``.
    """
    import gzip
    import json
    import pandas as pd

    inventory = load_ogn_inventory(root)
    if not inventory:
        return pd.DataFrame(columns=["hour", "n_aircraft", "n_packets"])
    if day is None:
        # Most recent file we have.
        stats = inventory[-1]
    else:
        stats = next((s for s in inventory if s.day == day), None)
        if stats is None:
            return pd.DataFrame(columns=["hour", "n_aircraft", "n_packets"])

    # Aircraft IDs and packet counts per hour. Receiver/status beacons
    # (lines starting with '#') are not aircraft and excluded from the
    # aircraft-count axis; they still count as packets.
    import re as _re

    per_hour_ids: dict[int, set[bytes]] = {}
    per_hour_packets: dict[int, int] = {}

    # Bypass json.loads entirely: extract ``hour`` and ``raw`` straight
    # from the JSON bytes with one compiled regex. The writer produces
    # a fixed, escape-free schema (raw APRS lines don't contain ``"``
    # except as JSON-escaped, which we don't match anyway), so the
    # regex is exact in practice. ~5× faster than per-line json.loads
    # on a 1 GB day file.
    pat = _re.compile(
        rb'"ts_recv":"\d{4}-\d{2}-\d{2}T(\d{2}):'  # hour
        rb'[^"]*","raw":"([^"]*)"'  # raw payload (no escaping in our writer)
    )

    # Hold a small overlap between chunks so a line straddling a chunk
    # boundary isn't missed. 4 KiB carries the longest sane APRS line
    # comfortably.
    leftover = b""
    for chunk in _iter_chunks(stats.path):
        data = leftover + chunk
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            leftover = data
            continue
        scan, leftover = data[: last_nl + 1], data[last_nl + 1 :]
        for match in pat.finditer(scan):
            hour = int(match.group(1))
            raw = match.group(2)
            per_hour_packets[hour] = per_hour_packets.get(hour, 0) + 1
            if not raw or raw.startswith(b"#"):
                continue
            gt = raw.find(b">")
            if gt > 0:
                per_hour_ids.setdefault(hour, set()).add(raw[:gt])

    rows = []
    for h in range(24):
        rows.append(
            {
                "hour": h,
                "n_aircraft": len(per_hour_ids.get(h, set())),
                "n_packets": per_hour_packets.get(h, 0),
            }
        )
    return pd.DataFrame(rows)
