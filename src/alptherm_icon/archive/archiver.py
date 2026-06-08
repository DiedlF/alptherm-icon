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

from alptherm_icon import monitoring
from alptherm_icon.archive import manifest, s3
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
    """(var, lead_h) -> local grib2 path; only populated for tier1 (zarr append)."""
    local_paths: list[Path] = field(default_factory=list)
    """Every successfully-downloaded local grib2 path, all tiers — for S3 raw upload.
    (``paths`` can't carry tier2 here: its (var, lead) key collides across the 65
    model levels of a profile.)"""
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

    def raw_key(self, local_path: Path) -> str:
        """S3 object key for a raw grib2 file: its path relative to the
        ``grib/`` tree, so the bucket mirrors the on-disk layout
        (``grib/YYYY/MM/DD/HH/tierN/<file>.grib2``)."""
        return local_path.resolve().relative_to(self.archive_dir.resolve()).as_posix()


def _upload_raw_to_s3(
    cfg: s3.S3Config,
    paths: ArchiveRoot,
    local_paths: list[Path],
) -> int:
    """Mirror freshly-downloaded raw grib2 files into the Object-Lock raw bucket.

    Best-effort and idempotent — already-present keys are skipped, per-file
    errors are logged but never abort the run (the local cache already holds
    the data; a failed upload retries on the next archive pass / migrate).
    Returns the number of files newly uploaded.
    """
    s3_client = s3.client(cfg)
    uploaded = 0
    for p in local_paths:
        key = paths.raw_key(p)
        try:
            if s3.upload_raw(cfg, p, key, s3=s3_client):
                uploaded += 1
        except Exception as exc:  # noqa: BLE001 — never block archival on upload
            log.warning("s3 raw upload failed for %s: %r", key, exc)
    return uploaded


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
        result.local_paths.append(path)
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
    cfg = s3.load_s3_config(root)
    init_utc = manifest.iso_z(init)
    hb_job = f"tier1-{init.hour:02d}"

    if manifest.has_record(paths.manifest_path, init_utc, "tier1") and not force:
        log.info("tier1 already recorded for %s — skipping", init_utc)
        monitoring.write(
            root=root,
            job=hb_job,
            status="skip",
            extra={"init_utc": init_utc, "reason": "already_recorded"},
        )
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

    # Mirror raw grib2 into the Object-Lock raw bucket (archive of record).
    if cfg is not None and result.local_paths:
        n_up = _upload_raw_to_s3(cfg, paths, result.local_paths)
        log.info("s3 raw upload: %d new file(s) → %s", n_up, cfg.raw_bucket)

    # Zarr append is best-effort — never block the archive on it. Writes to
    # the S3 zarr bucket when configured, else the local rolling cache.
    if not skip_zarr and result.files_ok > 0:
        zarr_target: Path | str = cfg.zarr_url if cfg is not None else paths.zarr_tier1_path
        zarr_opts = cfg.storage_options if cfg is not None else None
        try:
            n_steps = append_tier1_to_zarr(
                grib_paths=result.paths,
                zarr_target=zarr_target,
                init=init,
                storage_options=zarr_opts,
            )
            log.info("zarr append: %d time-steps written → %s", n_steps, zarr_target)
        except Exception as exc:  # noqa: BLE001
            log.warning("zarr append failed for %s: %r", init_utc, exc)

    # All-404 (e.g. run outside DWD's 48h window) is a defined failure
    # mode: the row gets persisted but the heartbeat reflects it, so the
    # dashboard and alerter can see the gap.
    hb_status = "ok" if result.files_ok > 0 else "fail"
    monitoring.write(
        root=root,
        job=hb_job,
        status=hb_status,
        extra={
            "init_utc": init_utc,
            "files_ok": result.files_ok,
            "files_404": result.files_404,
            "files_error": result.files_error,
            "bytes_on_disk": result.bytes_on_disk,
        },
    )
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
        monitoring.write(
            root=root,
            job="tier2-decision",
            status="skip",
            extra={"target_init_utc": target_init_utc, "reason": "already_recorded"},
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
        monitoring.write(
            root=root,
            job="tier2-decision",
            status="fail",
            extra={
                "target_init_utc": target_init_utc,
                "decision_init_utc": decision_init_utc,
                "reason": "no_trigger_inputs",
            },
        )
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
    monitoring.write(
        root=root,
        job="tier2-decision",
        status="ok",
        extra={
            "target_init_utc": target_init_utc,
            "decision_init_utc": decision_init_utc,
            "fire": decision.fire,
            "reason": decision.reason,
        },
    )
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
    cfg = s3.load_s3_config(root)
    pending = manifest.pending_tier2_targets(paths.manifest_path)
    if not pending:
        log.info("no pending tier2 downloads")
        monitoring.write(
            root=root,
            job="tier2-download",
            status="skip",
            extra={"pending": 0},
        )
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

        # Tier-2 full profiles are the monotonic grower (plan §9.6) — mirror
        # them straight into the Object-Lock raw bucket.
        if cfg is not None and result.local_paths:
            n_up = _upload_raw_to_s3(cfg, paths, result.local_paths)
            log.info("s3 raw upload: %d new file(s) → %s", n_up, cfg.raw_bucket)

    total_ok = sum(r.files_ok for r in written)
    total_bytes = sum(r.bytes_on_disk for r in written)
    monitoring.write(
        root=root,
        job="tier2-download",
        status="ok" if total_ok > 0 else "fail",
        extra={
            "targets": len(written),
            "files_ok_total": total_ok,
            "bytes_on_disk_total": total_bytes,
            "last_target_utc": written[-1].init_utc if written else None,
        },
    )
    return written


# ---------------------------------------------------------------------------
# One-time migration — push the existing local archive up to S3 (plan §9.6)
# ---------------------------------------------------------------------------


def _existing_raw_keys(cfg: s3.S3Config, s3_client) -> set[str]:
    """List every object key already under ``grib/`` in the raw bucket.

    One paginated LIST is far cheaper than a HEAD per local file when the
    migration is resumed — we diff locally and only upload what's missing.
    """
    keys: set[str] = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.raw_bucket, Prefix="grib/"):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def migrate_to_s3(
    root: Path,
    do_raw: bool = True,
    do_zarr: bool = True,
    workers: int = 24,
    log_every: int = 2000,
) -> dict[str, int]:
    """Upload the existing local ``data/archive/`` to S3 (one-time, resumable).

    - Raw grib2 → the Object-Lock raw bucket, skipping keys already present
      (so an interrupted run resumes cheaply). Uploads run on a thread pool
      because the cost is per-file round-trip latency, not throughput — the
      files are small (~2 MB) and Hetzner is happy with concurrent PUTs.
    - The local ``tier1.zarr`` → the zarr bucket via a recursive ``s3fs`` put.

    Returns a small stats dict. Idempotent: safe to re-run.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = s3.load_s3_config(root)
    if cfg is None:
        raise RuntimeError(
            "S3 not configured — set credentials in data/archive.env "
            "(see data/archive.env.example)"
        )
    paths = ArchiveRoot(root=root)
    stats = {"raw_uploaded": 0, "raw_skipped": 0, "raw_failed": 0, "zarr": 0}

    if do_raw:
        s3_client = s3.client(cfg)
        s3.ensure_raw_retention(cfg, s3=s3_client)  # default Governance lock on PUT
        log.info("migrate: listing existing raw keys …")
        existing = _existing_raw_keys(cfg, s3_client)
        log.info("migrate: %d raw keys already in bucket", len(existing))
        grib_root = paths.archive_dir / "grib"
        local_files = sorted(grib_root.rglob("*.grib2")) if grib_root.exists() else []
        todo = [(p, paths.raw_key(p)) for p in local_files if paths.raw_key(p) not in existing]
        stats["raw_skipped"] = len(local_files) - len(todo)
        log.info(
            "migrate: %d local raw files, %d already present, %d to upload (%d workers)",
            len(local_files), stats["raw_skipped"], len(todo), workers,
        )

        # boto3 clients aren't thread-safe; give each worker thread its own.
        _tls = threading.local()

        def _upload(item: tuple[Path, str]) -> str:
            p, key = item
            cl = getattr(_tls, "client", None)
            if cl is None:
                cl = _tls.client = s3.client(cfg)
            s3.upload_raw(cfg, p, key, s3=cl, check_exists=False)
            return key

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_upload, it): it for it in todo}
            for fut in as_completed(futures):
                done += 1
                try:
                    fut.result()
                    stats["raw_uploaded"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["raw_failed"] += 1
                    log.warning("migrate: raw upload failed for %s: %r", futures[fut][1], exc)
                if done % log_every == 0:
                    log.info(
                        "migrate raw: %d/%d (up=%d fail=%d)",
                        done, len(todo), stats["raw_uploaded"], stats["raw_failed"],
                    )

    if do_zarr:
        local_zarr = paths.zarr_tier1_path
        if local_zarr.exists():
            # boto3 (not s3fs) so request clock-skew is auto-corrected and a
            # failed run can't strand orphan chunks — see s3.upload_zarr_tree.
            zstats = s3.upload_zarr_tree(cfg, local_zarr, workers=workers)
            stats["zarr"] = 1 if not zstats["failed"] else 0
            stats["zarr_failed"] = zstats["failed"]
        else:
            log.info("migrate: no local zarr at %s — skipping", local_zarr)

    log.info("migrate done: %s", stats)
    return stats


def _init_date_from_raw_path(grib_root: Path, p: Path) -> dt.date | None:
    """Parse the init *date* from a raw grib path ``YYYY/MM/DD/HH/tierN/file``.

    Returns ``None`` if the path doesn't match the expected layout (such a
    file is treated as un-prunable — we never delete what we can't date).
    """
    try:
        rel = p.resolve().relative_to(grib_root.resolve()).parts
    except ValueError:
        return None
    if len(rel) < 4:
        return None
    try:
        return dt.date(int(rel[0]), int(rel[1]), int(rel[2]))
    except ValueError:
        return None


def prune_local(
    root: Path,
    keep_days: int = 4,
    apply: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Reclaim disk by deleting old local raw grib that is safely in S3.

    The local ``data/archive/grib`` tree is only a rolling working cache; S3
    is the archive of record. This trims it to the last ``keep_days`` of init
    dates, deleting older files **only after confirming the same key exists in
    the raw bucket** (one paginated LIST, then a local set-diff — never a blind
    delete). Files newer than the window, files we can't date, and files not
    yet confirmed in S3 are all left untouched.

    Dry-run by default: pass ``apply=True`` to actually unlink. The zarr is
    never pruned (it's a single append-only store, not a rolling cache).

    Returns a stats dict with counts and ``freed_bytes``.
    """
    cfg = s3.load_s3_config(root)
    if cfg is None:
        raise RuntimeError(
            "S3 not configured — refusing to prune the local cache without an "
            "archive of record. Set credentials in data/archive.env."
        )
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now.date() - dt.timedelta(days=keep_days)

    paths = ArchiveRoot(root=root)
    grib_root = paths.archive_dir / "grib"
    stats = {
        "scanned": 0, "kept_recent": 0, "undatable": 0,
        "unconfirmed": 0, "deleted": 0, "freed_bytes": 0,
    }
    if not grib_root.exists():
        log.info("prune: no local grib tree at %s — nothing to do", grib_root)
        return stats

    s3_client = s3.client(cfg)
    log.info("prune: listing raw keys already in s3://%s …", cfg.raw_bucket)
    existing = _existing_raw_keys(cfg, s3_client)
    log.info("prune: %d keys in bucket; keep_days=%d cutoff<%s apply=%s",
             len(existing), keep_days, cutoff.isoformat(), apply)

    local_files = sorted(grib_root.rglob("*.grib2"))
    for p in local_files:
        stats["scanned"] += 1
        init_date = _init_date_from_raw_path(grib_root, p)
        if init_date is None:
            stats["undatable"] += 1
            continue
        if init_date >= cutoff:
            stats["kept_recent"] += 1
            continue
        if paths.raw_key(p) not in existing:
            stats["unconfirmed"] += 1
            log.debug("prune: NOT in s3, keeping %s", paths.raw_key(p))
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if apply:
            try:
                p.unlink()
            except OSError as exc:  # noqa: BLE001
                log.warning("prune: failed to delete %s: %r", p, exc)
                continue
        stats["deleted"] += 1
        stats["freed_bytes"] += size

    if apply:
        _remove_empty_dirs(grib_root)
    log.info("prune %s done: %s", "APPLY" if apply else "DRY-RUN", stats)
    return stats


def _remove_empty_dirs(top: Path) -> None:
    """Recursively drop now-empty directories left behind after pruning."""
    for d in sorted((p for p in top.rglob("*") if p.is_dir()), reverse=True):
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
