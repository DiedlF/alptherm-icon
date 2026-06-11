"""Tracks aus dem OGN-Roh-Log assemblieren (plan §6.1/§9.5 → §6.2).

Liest ein Tages-Roh-Log (``data/ogn/raw/.../YYYY-MM-DD.jsonl.gz``),
parst die APRS-Positions-Beacons mit ``ogn-parser`` und gruppiert sie
pro Aircraft zu :class:`Track`-Objekten, die der Kreisdetektor
(:mod:`circling`) direkt verarbeitet.

Filter ab Werk:
- nur ``aprs_type == "position"`` (Receiver-/Status-Beacons raus),
- ADS-B-Verkehrsflugzeuge ausgeschlossen (kein Soaring; erkennbar am
  ``beacon_type``),
- optionales UTC-Stundenfenster (Default 8–17 UTC = Konvektionszeit) —
  hält Speicher und Laufzeit im Rahmen, da ein Tages-Log ~25 M Beacons
  hat.

Performance: gzip wird über ``pigz`` (falls vorhanden) chunked
dekomprimiert; jede Zeile ein ogn-parser-Call ist der Kostenfaktor, der
Hour-Filter + ADS-B-Ausschluss reduziert die geparste Menge deutlich.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from ogn.parser import parse as ogn_parse
from ogn.parser.exceptions import AprsParseError

from alptherm_icon.igc_pipeline.clean import clean_fixes
from alptherm_icon.igc_pipeline.track import (
    SOARING_AIRCRAFT_TYPES,
    Track,
)

log = logging.getLogger(__name__)


def _iter_raw_lines(path: Path, chunk_size: int = 8 << 20) -> Iterator[bytes]:
    """Yield raw bytes lines from the gzip log, pigz-accelerated, EOF-safe."""
    pigz = shutil.which("pigz")
    if pigz is None:
        try:
            with gzip.open(path, "rb") as fh:
                for line in fh:
                    yield line
        except EOFError:
            return
        return
    proc = subprocess.Popen([pigz, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    leftover = b""
    try:
        while True:
            chunk = proc.stdout.read(chunk_size)  # type: ignore[union-attr]
            if not chunk:
                break
            data = leftover + chunk
            nl = data.rfind(b"\n")
            if nl < 0:
                leftover = data
                continue
            body, leftover = data[: nl + 1], data[nl + 1 :]
            for line in body.split(b"\n"):
                if line:
                    yield line
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        proc.wait()


def assemble_tracks(
    log_path: Path,
    hour_window: tuple[int, int] | None = (8, 17),
    soaring_only: bool = True,
    min_fixes: int = 20,
    max_aircraft: int | None = None,
) -> list[Track]:
    """Parse one day's OGN log into per-aircraft tracks.

    Parameters
    ----------
    hour_window
        Inclusive UTC-hour range to keep (default 8–17, the convective
        window). ``None`` keeps all.
    soaring_only
        Keep only glider / hang-glider / paraglider (``aircraft_type``
        in :data:`SOARING_AIRCRAFT_TYPES`). Excludes the ADS-B/SafeSky
        powered traffic that otherwise enters as ``beacon_type=unknown``
        with ``aircraft_type`` 8/9 and pollutes the climb stats.
    min_fixes
        Tracks with fewer fixes are dropped (too short for circling).
    max_aircraft
        Optional cap (dev / smoke-test).
    """
    by_aircraft: dict[str, list[tuple]] = {}  # name → [(gps_dt, lat, lon, alt), …]
    type_of: dict[str, int | None] = {}
    h_lo, h_hi = hour_window if hour_window else (0, 23)
    n_seen = 0

    for raw in _iter_raw_lines(log_path):
        # Cheap prefilters before the (costly) parser.
        if b'"raw":"#' in raw:  # APRS server/status comment
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts_recv", "")
        if len(ts) < 13:
            continue
        try:
            hour = int(ts[11:13])
        except ValueError:
            continue
        if hour_window and not (h_lo <= hour <= h_hi):
            continue
        line = rec.get("raw", "")
        try:
            p = ogn_parse(line)
        except (AprsParseError, ValueError, KeyError):
            continue
        if not p or p.get("aprs_type") != "position":
            continue
        ac_type = p.get("aircraft_type")
        if soaring_only and ac_type not in SOARING_AIRCRAFT_TYPES:
            continue
        name = p.get("name")
        lat, lon, alt = p.get("latitude"), p.get("longitude"), p.get("altitude")
        if name is None or lat is None or lon is None or alt is None:
            continue
        # Order by GPS packet time, not receive time. The APRS timestamp carries
        # only HH:MM:SS (the parser stamps today's date), so recombine its
        # time-of-day with the log day from ts_recv.
        gps_ts = p.get("timestamp")
        try:
            day_date = dt.date.fromisoformat(ts[:10])
        except ValueError:
            continue
        if gps_ts is not None:
            gps_dt = dt.datetime.combine(day_date, gps_ts.timetz())
        else:
            try:
                gps_dt = dt.datetime.strptime(ts[:19] + "Z", "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                continue
        slot = by_aircraft.get(name)
        if slot is None:
            if max_aircraft is not None and len(by_aircraft) >= max_aircraft:
                continue
            slot = by_aircraft[name] = []
            type_of[name] = ac_type
        slot.append((gps_dt, float(lat), float(lon), float(alt)))
        n_seen += 1

    # Clean each aircraft's raw observations into a GPS-time-ordered, deduped,
    # jump-filtered track (the derived analysis layer — raw stays untouched).
    tracks = []
    for name, recs in by_aircraft.items():
        fixes = clean_fixes(recs)
        if len(fixes) >= min_fixes:
            tracks.append(Track(source_id=name, fixes=fixes, aircraft_type=type_of.get(name)))
    log.info(
        "assembled %d tracks (%d aircraft seen, %d fixes total) from %s",
        len(tracks),
        len(by_aircraft),
        n_seen,
        log_path.name,
    )
    return tracks
