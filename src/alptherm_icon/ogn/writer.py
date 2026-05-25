"""Day-rotating gzipped JSONL writer for the raw OGN APRS stream.

One line per received packet::

    {"ts_recv": "2026-05-25T19:42:13.117Z", "raw": "FLRDDxxxx>APRS,qAS,..."}

The receive timestamp (``ts_recv``) is the local arrival time and may
differ from the server-side packet timestamp by network latency — both
are useful, so the raw line is kept verbatim and ``ts_recv`` is added
on top as bookkeeping. The line itself is unparsed: parsing happens
later in the analysis layer (plan §9.5 "Rohschicht unveränderlich,
Auswerteschicht reproduzierbar").

The writer auto-rotates at UTC midnight: a packet that arrives at
00:00:01 UTC opens the new day's file and closes the previous day's.
gzip is chosen over zstd for stdlib-only deps; compression ratio on
APRS text is comfortably > 10x, which is enough for the expected
size budget (a few GB per saison).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


def raw_log_path(root: Path, day: dt.date) -> Path:
    """Resolve ``data/ogn/raw/YYYY/MM/YYYY-MM-DD.jsonl.gz``.

    Nested month directories keep ``ls`` usable as the archive grows.
    """
    return (
        root
        / "data"
        / "ogn"
        / "raw"
        / f"{day.year:04d}"
        / f"{day.month:02d}"
        / f"{day:%Y-%m-%d}.jsonl.gz"
    )


class DailyRawLogWriter:
    """Append-only writer that rotates to a new gzipped file each UTC day.

    Designed for high-frequency, small-record workloads (one APRS packet
    per call, ~100–200 B raw). The writer:

    - opens with ``mode="ab"`` so an interrupted day picks up cleanly,
    - flushes after every write (gzip's internal buffer is preserved
      across calls — full compression efficiency, but a Ctrl-C never
      loses more than what's in the gzip frame buffer),
    - tracks ``bytes_written`` for the heartbeat extra payload.

    Not thread-safe; assume one-writer-many-readers like the manifest.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._current_day: dt.date | None = None
        self._fh: IO[bytes] | None = None
        self.bytes_written_today: int = 0
        self.packets_written_today: int = 0

    def _open_for(self, day: dt.date) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        path = raw_log_path(self.root, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append-binary: an existing file from earlier today is extended,
        # not overwritten. gzip handles concatenated frames natively.
        self._fh = gzip.open(path, mode="ab")
        self._current_day = day
        # Reset per-day counters only when rolling into a new day.
        self.bytes_written_today = path.stat().st_size if path.exists() else 0
        self.packets_written_today = 0
        log.info("ogn: opened %s (existing size=%d B)", path, self.bytes_written_today)

    def write(self, raw_line: str, ts_recv: dt.datetime | None = None) -> None:
        """Append one APRS packet to the current day's log."""
        ts_recv = ts_recv or dt.datetime.now(dt.timezone.utc)
        if ts_recv.tzinfo is None:
            ts_recv = ts_recv.replace(tzinfo=dt.timezone.utc)
        day = ts_recv.date()
        if day != self._current_day:
            self._open_for(day)
        assert self._fh is not None
        record = {
            "ts_recv": ts_recv.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_recv.microsecond // 1000:03d}Z",
            "raw": raw_line,
        }
        encoded = json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        self._fh.write(encoded)
        self.bytes_written_today += len(encoded)
        self.packets_written_today += 1

    def flush(self) -> None:
        """Force gzip-buffer flush to disk. Cheap to call frequently."""
        if self._fh is None:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except (OSError, AttributeError):
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._current_day = None

    # Context-manager sugar for tests / one-shot usage.
    def __enter__(self) -> "DailyRawLogWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
