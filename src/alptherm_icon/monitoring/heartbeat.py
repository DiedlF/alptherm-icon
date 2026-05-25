"""Atomic per-job status files for the monitoring/alerting pipeline (§10.1).

Each archive cronjob and the OGN logger call ``write(job, status, extra)``
after an attempt; the writer:

- updates ``last_attempt_utc`` on every call,
- updates ``last_success_utc`` only on ``status == "ok"``,
- bumps ``since_utc`` when the status string changes (so alerting can ask
  "how long has this job been failing?" instead of just "is it failing?"),
- writes atomically (``tmp + rename``) so a concurrent reader never sees
  half-written JSON.

Status convention:
- ``"ok"`` — the job succeeded
- ``"skip"`` — the job was a no-op (idempotent: nothing new to do)
- ``"fail"`` — the job ran but reported errors (per-file 404s tolerated)
- ``"crash"`` — the job died with an exception (caller responsible for
  catching at the outer layer)

The reader API (``read_all``) returns a list of ``HeartbeatStatus`` records
sorted alphabetically by job name — exactly the shape the FastAPI status
endpoint and the ntfy alerter want.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUS_DIR_NAME = "status"


@dataclass
class HeartbeatStatus:
    """One job's persisted state. Stored as ``data/status/{job}.json``."""

    job: str
    last_attempt_utc: str
    last_status: str
    since_utc: str
    last_success_utc: str | None = None
    last_extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HeartbeatStatus":
        return cls(
            job=d["job"],
            last_attempt_utc=d["last_attempt_utc"],
            last_status=d["last_status"],
            since_utc=d["since_utc"],
            last_success_utc=d.get("last_success_utc"),
            last_extra=d.get("last_extra") or {},
        )

    def to_json(self) -> str:
        d = {k: v for k, v in dataclasses.asdict(self).items() if v is not None}
        return json.dumps(d, indent=2, sort_keys=True)


def _iso_z(t: dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_path(root: Path, job: str) -> Path:
    """Resolve ``data/status/{job}.json`` under the project root."""
    return root / "data" / STATUS_DIR_NAME / f"{job}.json"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write(
    root: Path,
    job: str,
    status: str,
    extra: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> HeartbeatStatus:
    """Update ``data/status/{job}.json`` atomically and return the new record.

    Parameters
    ----------
    root
        Project root (the directory containing ``pyproject.toml``).
    job
        Short identifier for the heartbeat, e.g. ``"tier1-06"``,
        ``"tier2-decision"``, ``"ogn-stream"``. Used as both filename
        and ``HeartbeatStatus.job``.
    status
        One of ``ok`` / ``skip`` / ``fail`` / ``crash``. Anything else
        is accepted but should be defined first.
    extra
        Optional job-specific payload (file counts, byte sums, trigger
        reason, …). Overwrites the previous ``last_extra`` rather than
        merging — callers pass the full snapshot each call.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    now_iso = _iso_z(now)
    path = status_path(root, job)
    prev: HeartbeatStatus | None = None
    try:
        prev = read(root, job)
    except FileNotFoundError:
        prev = None

    since_utc = now_iso
    last_success = None
    if prev is not None:
        last_success = prev.last_success_utc
        if prev.last_status == status:
            since_utc = prev.since_utc  # same state, preserve original timestamp

    if status == "ok":
        last_success = now_iso

    record = HeartbeatStatus(
        job=job,
        last_attempt_utc=now_iso,
        last_status=status,
        since_utc=since_utc,
        last_success_utc=last_success,
        last_extra=extra or {},
    )
    _atomic_write(path, record.to_json())
    return record


def read(root: Path, job: str) -> HeartbeatStatus:
    """Load one job's heartbeat. Raises ``FileNotFoundError`` if absent."""
    path = status_path(root, job)
    d = json.loads(path.read_text(encoding="utf-8"))
    return HeartbeatStatus.from_dict(d)


def read_all(root: Path) -> list[HeartbeatStatus]:
    """Load every heartbeat under ``data/status/``, sorted by job name."""
    status_dir = root / "data" / STATUS_DIR_NAME
    if not status_dir.exists():
        return []
    out: list[HeartbeatStatus] = []
    for path in sorted(status_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            out.append(HeartbeatStatus.from_dict(d))
        except (json.JSONDecodeError, KeyError):
            continue  # ignore corrupt / unrecognized files
    return out
