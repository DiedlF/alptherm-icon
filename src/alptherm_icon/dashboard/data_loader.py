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

    # Performance: with ~25 M lines/day, line-iteration is the bottleneck.
    # We open in binary mode and substring-prefilter before json.loads —
    # only candidate lines pay the parse cost. A full scan still takes
    # ~50 s for a busy day, so the dashboard caches calls aggressively
    # (TTL = 120 s) and shows a spinner on first load. A future
    # optimisation (chunked regex over the decompressed stream) is
    # worthwhile but out of scope for the v0.4 dashboard milestone.
    try:
        with gzip.open(stats.path, "rb") as fh:
            for line_bytes in fh:
                if not any(s in line_bytes for s in interesting_bytes):
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
    except EOFError:
        pass

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
    per_hour_ids: dict[int, set[str]] = {}
    per_hour_packets: dict[int, int] = {}
    # The OGN daemon writes the current day's file live; mid-read EOFError
    # is expected on a partial gzip frame. Tolerate by breaking the loop
    # — we keep all already-read lines.
    try:
        with gzip.open(stats.path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("ts_recv", "")
                raw = rec.get("raw", "")
                if len(ts_str) < 13:
                    continue
                try:
                    hour = int(ts_str[11:13])
                except ValueError:
                    continue
                per_hour_packets[hour] = per_hour_packets.get(hour, 0) + 1
                if not raw or raw.startswith("#"):
                    continue
                # Aircraft ID is everything before the first '>' in APRS.
                gt = raw.find(">")
                if gt > 0:
                    per_hour_ids.setdefault(hour, set()).add(raw[:gt])
    except EOFError:
        # Live-writer left an incomplete gzip frame at EOF — done reading.
        pass

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
