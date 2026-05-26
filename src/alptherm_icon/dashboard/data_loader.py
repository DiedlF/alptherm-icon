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
