"""Append-only JSONL manifest for the M0 archive (plan §9.3 Metadaten-Log).

One record per (init, tier) attempt. Records are flushed and fsynced
after each write so a Ctrl-C / OOM kill doesn't leave the log behind
the on-disk state. Recovery semantics rely on this: the orchestrator
treats *any* completed record (including ones with ``errors``) as
"this tier was already attempted today" and skips re-downloading.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ManifestRecord:
    """One archive attempt — written once per (init, tier)."""

    init_utc: str  # "YYYY-MM-DDTHH:MM:SSZ"
    tier: str  # "tier1" | "tier2"
    finished_utc: str
    files_attempted: int
    files_ok: int
    files_404: int
    files_error: int
    bytes_on_disk: int
    variables: list[str]
    # Set on tier2_decision + tier2 records. The same dict is copied from
    # the decision to the eventual download so we can read either alone.
    trigger: dict[str, Any] | None = None
    # Which run was used to evaluate the trigger. For tier2_decision and
    # tier2, this is typically the 09-UTC-Lauf while init_utc is the
    # 06-UTC target. None for tier1 records.
    decision_init_utc: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        # Omit None-valued optional fields so tier1 rows don't carry
        # tier2-specific clutter (``trigger``, ``decision_init_utc``).
        # ``read_all`` consumers already use ``.get(...)`` so the absent
        # keys are interchangeable with ``null``.
        d = {k: v for k, v in dataclasses.asdict(self).items() if v is not None}
        return json.dumps(d, separators=(",", ":"), sort_keys=True)


def iso_z(t: dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(record: ManifestRecord, path: Path) -> None:
    """Append one record, flushing to disk before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.to_json())
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_all(path: Path) -> list[dict[str, Any]]:
    """Parse the full manifest. Returns ``[]`` if the file doesn't exist yet."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def has_record(path: Path, init_utc: str, tier: str) -> bool:
    """True iff a record for this (init, tier) already exists."""
    for row in read_all(path):
        if row.get("init_utc") == init_utc and row.get("tier") == tier:
            return True
    return False


def pending_tier2_targets(path: Path) -> list[dict[str, Any]]:
    """Return tier2_decision records with fire=True that lack a tier2 download.

    The trigger command writes a tier2_decision record keyed on the target
    init (06 UTC). The nightly download-pending command consumes these and
    promotes them into full tier2 records once the GRIBs are on disk.
    Records are returned in chronological order (by init_utc).
    """
    rows = read_all(path)
    fired: dict[str, dict[str, Any]] = {}
    downloaded: set[str] = set()
    for r in rows:
        tier = r.get("tier")
        if tier == "tier2_decision" and (r.get("trigger") or {}).get("fire"):
            # Later decisions override earlier ones for the same target
            # (a re-run with --force should win).
            fired[r["init_utc"]] = r
        elif tier == "tier2":
            downloaded.add(r["init_utc"])
    return [v for k, v in sorted(fired.items()) if k not in downloaded]
