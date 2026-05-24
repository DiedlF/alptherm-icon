"""M0 archive orchestrator — Tier 1 sammlung, Tier 2 decide, Tier 2 download.

Layout under ``<root>/data/archive/``::

    grib/<YYYY>/<MM>/<DD>/<HH>/tier1/*.grib2
    grib/<YYYY>/<MM>/<DD>/<HH>/tier2/*.grib2          # only after pending download
    zarr/tier1.zarr/                                   # daily-appended surface stack
    manifest.jsonl                                     # one row per (init, tier)

Per plan §9.2 the flow is three-stage and the stages run from different
cronjobs:

1. ``archive_tier1(init)`` — pulls the ~15 surface vars for one ICON-D2
   run and appends to Zarr. Called four times a day, once per
   00/03/06/09 UTC anchor, from the matching Tier-1 cronjob.

2. ``decide_tier2(decision_init=today-09Z, target_init=today-06Z)`` —
   diagnostic trigger: evaluates the 09-UTC-Lauf's already-archived
   Tier-1 GRIBs at small leads (afternoon convection ~2 h ahead) and
   writes a ``tier2_decision`` manifest record naming the 06-UTC target.
   Called once a day at ~12 UTC.

3. ``download_pending_tier2()`` — finds every fired ``tier2_decision``
   that lacks a matching ``tier2`` download record and pulls the full
   model-level profile of the 06-UTC-Lauf. Called once a day at night,
   when bandwidth is free.

Idempotent throughout — each stage is a no-op if its manifest record
is already present (unless ``force=True``).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from alptherm_icon.archive import manifest
from alptherm_icon.archive.trigger import (
    TRIGGER_LEAD_RANGE,
    TriggerDecision,
    evaluate,
)
from alptherm_icon.archive.variables import (
    TIER1_SURFACE_VARS,
    TIER2_PROFILE_VARS,
    TRIGGER_VARS,
    tier1_specs,
    tier2_specs,
)
from alptherm_icon.archive.zarr_append import append_tier1_to_zarr
from alptherm_icon.icon_pipeline.icon import IconD2File, download_and_decompress

log = logging.getLogger(__name__)


@dataclass
class TierResult:
    tier: str
    files_attempted: int
    files_ok: int
    files_404: int
    files_error: int
    bytes_on_disk: int
    paths: dict[tuple[str, int], Path] = field(default_factory=dict)
    """(var, lead_h) -> local grib2 path; only populated for tier1."""
    errors: list[str] = field(default_factory=list)


@dataclass
class ArchiveRoot:
    """Resolves on-disk paths under ``<root>/data/archive/``."""

    root: Path

    @property
    def archive_dir(self) -> Path:
        return self.root / "data" / "archive"

    def grib_dir(self, init: dt.datetime, tier: str) -> Path:
        return (
            self.archive_dir
            / "grib"
            / f"{init.year:04d}"
            / f"{init.month:02d}"
            / f"{init.day:02d}"
            / f"{init.hour:02d}"
            / tier
        )

    @property
    def manifest_path(self) -> Path:
        return self.archive_dir / "manifest.jsonl"

    @property
    def zarr_tier1_path(self) -> Path:
        return self.archive_dir / "zarr" / "tier1.zarr"


def _ensure_utc(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _download_job(
    specs: tuple[IconD2File, ...],
    cache_dir: Path,
    tier: str,
    sleep_between_s: float = 0.1,
) -> TierResult:
    """Run a download job sequentially. Tolerates 404s and per-file errors."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = TierResult(
        tier=tier,
        files_attempted=len(specs),
        files_ok=0,
        files_404=0,
        files_error=0,
        bytes_on_disk=0,
    )
    for spec in specs:
        try:
            path = download_and_decompress(spec, cache_dir)
        except Exception as exc:  # noqa: BLE001 — single missing file shouldn't kill the run
            result.files_error += 1
            result.errors.append(f"{spec.filename_grib2}: {exc!r}")
            continue
        if path is None:
            result.files_404 += 1
            continue
        result.files_ok += 1
        try:
            result.bytes_on_disk += path.stat().st_size
        except OSError:
            pass
        if tier == "tier1":
            result.paths[(spec.var, spec.lead_h)] = path
        if sleep_between_s > 0:
            time.sleep(sleep_between_s)
    return result


def _result_to_record(
    init_utc: str,
    result: TierResult,
    variables: list[str],
    trigger: TriggerDecision | dict | None = None,
    decision_init_utc: str | None = None,
) -> manifest.ManifestRecord:
    if isinstance(trigger, TriggerDecision):
        trigger_dict = trigger.to_dict()
    else:
        trigger_dict = trigger
    return manifest.ManifestRecord(
        init_utc=init_utc,
        tier=result.tier,
        finished_utc=manifest.iso_z(dt.datetime.now(dt.timezone.utc)),
        files_attempted=result.files_attempted,
        files_ok=result.files_ok,
        files_404=result.files_404,
        files_error=result.files_error,
        bytes_on_disk=result.bytes_on_disk,
        variables=variables,
        trigger=trigger_dict,
        decision_init_utc=decision_init_utc,
        errors=result.errors[:50],
    )


def _ensure_trigger_inputs(
    decision_init: dt.datetime,
    cache_dir: Path,
    lead_range: tuple[int, int],
    sleep_between_s: float = 0.1,
) -> dict[tuple[str, int], Path]:
    """Make sure the small (var, lead) set the trigger needs is on disk.

    Fetches TRIGGER_VARS × leads in ``lead_range`` from the decision run.
    Files land in the same cache directory the full Tier-1 archive uses,
    so a later ``archive_tier1`` for the same init becomes a no-op for
    these files (``download_and_decompress`` is content-idempotent).

    Returns the ``(var, lead) -> Path`` map of files that ended up on
    disk; 404s and per-file errors are silently dropped.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    lead_lo, lead_hi = lead_range
    paths: dict[tuple[str, int], Path] = {}
    for var in TRIGGER_VARS:
        for lead in range(lead_lo, lead_hi + 1):
            spec = IconD2File(init=decision_init, lead_h=lead, var=var)
            try:
                p = download_and_decompress(spec, cache_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("trigger fetch failed %s: %r", spec.filename_grib2, exc)
                continue
            if p is not None:
                paths[(var, lead)] = p
            if sleep_between_s > 0:
                time.sleep(sleep_between_s)
    return paths


# ---------------------------------------------------------------------------
# Stage 1 — Tier 1 daily collection
# ---------------------------------------------------------------------------


def archive_tier1(
    init: dt.datetime,
    root: Path,
    lead_max: int = 48,
    sleep_between_s: float = 0.1,
    skip_zarr: bool = False,
    force: bool = False,
) -> manifest.ManifestRecord | None:
    """Archive Tier 1 for one ICON-D2 run end-to-end.

    Idempotent — if the manifest already has a tier1 record for this init,
    returns ``None`` without re-downloading (unless ``force=True``).
    """
    init = _ensure_utc(init)
    paths = ArchiveRoot(root=root)
    paths.archive_dir.mkdir(parents=True, exist_ok=True)
    init_utc = manifest.iso_z(init)

    if manifest.has_record(paths.manifest_path, init_utc, "tier1") and not force:
        log.info("tier1 already recorded for %s — skipping", init_utc)
        return None

    log.info("tier1 start for %s", init_utc)
    specs = tier1_specs(init, lead_max=lead_max)
    result = _download_job(
        specs=specs,
        cache_dir=paths.grib_dir(init, "tier1"),
        tier="tier1",
        sleep_between_s=sleep_between_s,
    )
    record = _result_to_record(init_utc, result, variables=list(TIER1_SURFACE_VARS))
    manifest.append(record, paths.manifest_path)
    log.info(
        "tier1 done: ok=%d, 404=%d, error=%d, bytes=%d",
        result.files_ok,
        result.files_404,
        result.files_error,
        result.bytes_on_disk,
    )

    # Zarr append is best-effort — never block the archive on it.
    if not skip_zarr and result.files_ok > 0:
        try:
            n_steps = append_tier1_to_zarr(
                grib_paths=result.paths,
                zarr_path=paths.zarr_tier1_path,
                init=init,
            )
            log.info("zarr append: %d time-steps written", n_steps)
        except Exception as exc:  # noqa: BLE001
            log.warning("zarr append failed for %s: %r", init_utc, exc)

    return record


# ---------------------------------------------------------------------------
# Stage 2 — Tier 2 diagnostic decision (12 UTC cronjob)
# ---------------------------------------------------------------------------


def decide_tier2(
    decision_init: dt.datetime,
    target_init: dt.datetime,
    root: Path,
    lead_range: tuple[int, int] = TRIGGER_LEAD_RANGE,
    force: bool = False,
) -> manifest.ManifestRecord | None:
    """Run the gut-day trigger and persist a ``tier2_decision`` record.

    Parameters
    ----------
    decision_init
        The ICON-D2 run whose Tier-1 GRIBs we evaluate (production: today
        09 UTC). Must already be on disk from an earlier ``archive_tier1``
        call — we don't re-fetch.
    target_init
        The run whose Tier-2 Vollprofile we want to archive if the
        trigger fires (production: today 06 UTC). Used as the manifest
        ``init_utc`` so a later ``tier2`` download keys onto the same row.
    lead_range
        Lead window to evaluate, in hours past ``decision_init``.
        Defaults to ``TRIGGER_LEAD_RANGE`` = (1, 6) for a 09-UTC decision
        run (10–15 UTC = early-afternoon convection).
    """
    decision_init = _ensure_utc(decision_init)
    target_init = _ensure_utc(target_init)
    paths = ArchiveRoot(root=root)
    target_init_utc = manifest.iso_z(target_init)
    decision_init_utc = manifest.iso_z(decision_init)

    if (
        manifest.has_record(paths.manifest_path, target_init_utc, "tier2_decision")
        and not force
    ):
        log.info(
            "tier2_decision already recorded for target %s — skipping",
            target_init_utc,
        )
        return None

    # Self-fetch the small trigger subset. Idempotent w.r.t. a later full
    # tier1 archival — the files land in the shared cache and won't be
    # re-downloaded. Decouples this command from the tier1-09 cronjob.
    decision_t1_dir = paths.grib_dir(decision_init, "tier1")
    grib_paths = _ensure_trigger_inputs(
        decision_init=decision_init,
        cache_dir=decision_t1_dir,
        lead_range=lead_range,
    )
    if not grib_paths:
        raise FileNotFoundError(
            f"no trigger inputs available for {decision_init_utc} — "
            "check opendata.dwd.de reachability"
        )

    decision = evaluate(grib_paths, lead_range=lead_range)
    log.info(
        "tier2_decision target=%s decision=%s fire=%s reason=%s",
        target_init_utc,
        decision_init_utc,
        decision.fire,
        decision.reason,
    )

    record = manifest.ManifestRecord(
        init_utc=target_init_utc,
        tier="tier2_decision",
        finished_utc=manifest.iso_z(dt.datetime.now(dt.timezone.utc)),
        files_attempted=0,
        files_ok=0,
        files_404=0,
        files_error=0,
        bytes_on_disk=0,
        variables=[],
        trigger=decision.to_dict(),
        decision_init_utc=decision_init_utc,
    )
    manifest.append(record, paths.manifest_path)
    return record


# ---------------------------------------------------------------------------
# Stage 3 — Nightly Tier 2 download (23 UTC cronjob)
# ---------------------------------------------------------------------------


def download_pending_tier2(
    root: Path,
    sleep_between_s: float = 0.1,
) -> list[manifest.ManifestRecord]:
    """Download Tier 2 for every fired decision that has no tier2 record yet.

    Walks the manifest, finds ``tier2_decision`` entries with ``fire=True``
    that lack a matching ``tier2`` row, and pulls the full model-level
    profile for each. Returns the new tier2 records written.

    If a tier2_decision references a target whose 06-UTC GRIBs have
    rolled out of the DWD window, ``_download_job`` will simply record
    404s — the tier2 record still gets written so the pending row clears.
    """
    paths = ArchiveRoot(root=root)
    pending = manifest.pending_tier2_targets(paths.manifest_path)
    if not pending:
        log.info("no pending tier2 downloads")
        return []

    written: list[manifest.ManifestRecord] = []
    for decision in pending:
        target_init_utc = decision["init_utc"]
        target_init = dt.datetime.strptime(
            target_init_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        log.info(
            "tier2 download start: target=%s (decided from %s)",
            target_init_utc,
            decision.get("decision_init_utc"),
        )
        specs = tier2_specs(target_init)
        result = _download_job(
            specs=specs,
            cache_dir=paths.grib_dir(target_init, "tier2"),
            tier="tier2",
            sleep_between_s=sleep_between_s,
        )
        record = _result_to_record(
            init_utc=target_init_utc,
            result=result,
            variables=[*TIER2_PROFILE_VARS, "hhl"],
            trigger=decision.get("trigger"),
            decision_init_utc=decision.get("decision_init_utc"),
        )
        manifest.append(record, paths.manifest_path)
        written.append(record)
        log.info(
            "tier2 done: target=%s ok=%d 404=%d error=%d bytes=%d",
            target_init_utc,
            result.files_ok,
            result.files_404,
            result.files_error,
            result.bytes_on_disk,
        )
    return written
