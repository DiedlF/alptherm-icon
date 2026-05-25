"""OGN APRS-Stream daemon — receives, logs raw, writes heartbeat.

Long-running process (not cron). Wraps the upstream ``ogn-client``
``AprsClient`` with three responsibilities:

- *Raw logging* via :class:`DailyRawLogWriter` (one gzipped JSONL per day),
- *Heartbeat* via :mod:`alptherm_icon.monitoring` every keepalive cycle
  (~240 s), with a 'fail' fallback when no packets arrived since the
  previous heartbeat — that's the only way to distinguish "stream
  silent" from "process alive but isolated",
- *Auto-reconnect* (delegated to AprsClient; ~100 retries × 15 s).

The geofilter is built from a ``BBox`` so the Alps mask used by the
ICON archive also defines what we ingest from OGN — a single source
of truth for the Alpenraum geographically.

The daemon is intentionally stateless beyond the writer + heartbeat
files. Restarting it picks up the same day's gzip file in append
mode; a missing crontab entry doesn't matter because OGN runs as a
systemd unit.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ogn.client import AprsClient

from alptherm_icon import monitoring
from alptherm_icon.archive.bbox import ALPEN_BBOX, BBox
from alptherm_icon.ogn.writer import DailyRawLogWriter

log = logging.getLogger(__name__)

HEARTBEAT_JOB = "ogn-stream"


@dataclass
class GeoFilter:
    """APRS range filter ``r/lat/lon/radius_km`` for a lat/lon Bbox.

    APRS supports only a circle filter on port 14580. We pick the
    smallest circle that contains the rectangular ``BBox``, computed
    in great-circle approximation. A small over-fetch (corners outside
    the bbox) is fine — the analysis layer trims geographically later.
    """

    lat: float
    lon: float
    radius_km: float

    @classmethod
    def for_bbox(cls, bbox: BBox, margin_km: float = 25.0) -> "GeoFilter":
        center_lat = (bbox.lat_min + bbox.lat_max) / 2
        center_lon = (bbox.lon_min + bbox.lon_max) / 2
        # Equirectangular distance to the bbox corner (close enough at
        # mid-latitudes for radius sizing; not used for actual geometry).
        dlat_km = (bbox.lat_max - center_lat) * 111.0
        dlon_km = (bbox.lon_max - center_lon) * 111.0 * math.cos(math.radians(center_lat))
        radius_km = math.hypot(dlat_km, dlon_km) + margin_km
        return cls(lat=center_lat, lon=center_lon, radius_km=math.ceil(radius_km))

    def to_aprs(self) -> str:
        # APRS server expects degrees and km, no decimals on the radius.
        return f"r/{self.lat:.4f}/{self.lon:.4f}/{int(self.radius_km)}"


class OgnDaemon:
    """Owns the AprsClient lifecycle, the writer, and the heartbeat."""

    def __init__(
        self,
        root: Path,
        bbox: BBox = ALPEN_BBOX,
        aprs_user: str = "N0CALL",
        flush_every: int = 200,
    ) -> None:
        self.root = root
        self.geofilter = GeoFilter.for_bbox(bbox)
        self.aprs_user = aprs_user
        self.flush_every = flush_every

        self._writer = DailyRawLogWriter(root=root)
        self._client = AprsClient(
            aprs_user=aprs_user,
            aprs_filter=self.geofilter.to_aprs(),
        )

        # In-memory counters reset by the heartbeat tick.
        self._received_since_heartbeat = 0
        self._received_total = 0
        self._unflushed_since_flush = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # AprsClient callbacks
    # ------------------------------------------------------------------

    def _on_packet(self, raw_line: str) -> None:
        if not raw_line:
            return
        # APRS server status lines (begin with '#') are kept too —
        # they document server-side filter, keepalive, version etc.
        try:
            self._writer.write(raw_line)
        except Exception as exc:  # noqa: BLE001
            log.error("ogn: write failed for line len=%d: %r", len(raw_line), exc)
            return
        self._received_since_heartbeat += 1
        self._received_total += 1
        self._unflushed_since_flush += 1
        if self._unflushed_since_flush >= self.flush_every:
            self._writer.flush()
            self._unflushed_since_flush = 0

    def _on_keepalive(self, client: AprsClient) -> None:
        """Called by AprsClient every APRS_KEEPALIVE_TIME (~240 s)."""
        self._writer.flush()
        # 'ok' if any packets arrived since the previous tick, 'fail'
        # otherwise — distinguishes "silent stream" from "dead daemon".
        status = "ok" if self._received_since_heartbeat > 0 else "fail"
        monitoring.write(
            root=self.root,
            job=HEARTBEAT_JOB,
            status=status,
            extra={
                "received_since_last_heartbeat": self._received_since_heartbeat,
                "received_total": self._received_total,
                "bytes_written_today": self._writer.bytes_written_today,
                "packets_written_today": self._writer.packets_written_today,
                "geofilter": self.geofilter.to_aprs(),
            },
        )
        self._received_since_heartbeat = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect with autoreconnect and pump until SIGINT/SIGTERM."""
        self._install_signal_handlers()
        log.info(
            "ogn: connecting as %s with filter %s",
            self.aprs_user,
            self.geofilter.to_aprs(),
        )
        # Initial 'skip' heartbeat so the dashboard immediately sees
        # the daemon is up — flips to 'ok' on the first keepalive
        # (after ~240 s, once packets have arrived). Alerter logic
        # should rely on staleness of last_attempt_utc, not on the
        # status value, to catch silent stalls.
        monitoring.write(
            root=self.root,
            job=HEARTBEAT_JOB,
            status="skip",
            extra={
                "reason": "starting",
                "geofilter": self.geofilter.to_aprs(),
            },
        )
        self._client.connect(retries=100, wait_period=15)
        try:
            self._client.run(
                callback=lambda line: self._on_packet(line),
                timed_callback=self._on_keepalive,
                autoreconnect=True,
            )
        finally:
            self._writer.flush()
            self._writer.close()
            monitoring.write(
                root=self.root,
                job=HEARTBEAT_JOB,
                status="crash" if not self._stop.is_set() else "skip",
                extra={
                    "received_total": self._received_total,
                    "reason": "shutdown",
                },
            )
            log.info("ogn: shutdown done (received_total=%d)", self._received_total)

    def stop(self, *args: Any) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("ogn: stop requested, disconnecting…")
        try:
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning("ogn: disconnect raised: %r", exc)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except ValueError:
                # Some environments (e.g. non-main threads) reject this; non-fatal.
                pass
