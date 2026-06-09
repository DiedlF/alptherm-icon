"""S3-compatible object-storage backend for the archive (plan §9.6).

The archive is split across two private Hetzner Object Storage buckets:

- ``*-raw`` (Object Lock, Governance) — append-only raw GRIB2. The
  unrecoverable, volatile layer; WORM-protected against accidental
  deletion and buggy cleanup scripts.
- ``*-zarr`` (no lock) — the derived, daily-appended Zarr archive.
  Reproducible from raw, so its safety lies in reproducibility, not a
  lock (a lock would block the daily append).

Local disk under ``data/archive/`` stays as a rolling working cache; S3
is the *archive of record* (plan §9.6 VPS target shape). From the rest
of the code's view the split is just a path prefix.

Configuration lives in ``data/archive.env`` (gitignored, same KEY=VALUE
format as ``data/monitoring.env``). If the essential keys are missing,
:func:`load_s3_config` returns ``None`` and the archive runs local-only —
so nothing breaks on a box that hasn't been configured for S3.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Object key of the Tier-1 surface Zarr within the zarr bucket. Mirrors the
# local ``data/archive/zarr/tier1.zarr`` leaf so raw and derived layouts read
# the same on disk and in the bucket.
ZARR_TIER1_PREFIX = "tier1.zarr"


def _load_env_file(path: Path) -> dict[str, str]:
    """Read a tiny ``KEY=VALUE``-per-line env file (see ``archive.env.example``).

    Same minimal format as the monitoring loader: no quoting beyond a
    surrounding pair of quotes, no expansion — keeps secrets out of the
    crontab without pulling in python-dotenv.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


@dataclass(frozen=True)
class S3Config:
    """Resolved Hetzner Object Storage connection + bucket layout."""

    endpoint_url: str
    region: str
    access_key: str
    secret_key: str
    raw_bucket: str
    zarr_bucket: str
    raw_retention_days: int = 365

    @property
    def zarr_url(self) -> str:
        return f"s3://{self.zarr_bucket}/{ZARR_TIER1_PREFIX}"

    @property
    def storage_options(self) -> dict:
        """``storage_options`` for xarray/fsspec/s3fs against this endpoint."""
        return {
            "key": self.access_key,
            "secret": self.secret_key,
            "client_kwargs": {
                "endpoint_url": self.endpoint_url,
                "region_name": self.region,
            },
        }


def load_s3_config(root: Path | None = None) -> S3Config | None:
    """Build an :class:`S3Config` from ``data/archive.env`` + process env.

    Resolution order: env file > process env. Returns ``None`` (local-only
    mode) if any of endpoint, both keys, or both bucket names are missing.
    """
    file_env: dict[str, str] = {}
    if root is not None:
        file_env = _load_env_file(root / "data" / "archive.env")

    def _get(key: str, default: str | None = None) -> str | None:
        if key in file_env:
            return file_env[key]
        return os.environ.get(key, default)

    endpoint = _get("ALPTHERM_S3_ENDPOINT")
    access_key = _get("ALPTHERM_S3_ACCESS_KEY")
    secret_key = _get("ALPTHERM_S3_SECRET_KEY")
    raw_bucket = _get("ALPTHERM_S3_RAW_BUCKET")
    zarr_bucket = _get("ALPTHERM_S3_ZARR_BUCKET")

    if not (endpoint and access_key and secret_key and raw_bucket and zarr_bucket):
        return None

    retention_raw = _get("ALPTHERM_S3_RAW_RETENTION_DAYS", "365") or "365"
    try:
        retention = int(retention_raw)
    except ValueError:
        log.warning("invalid ALPTHERM_S3_RAW_RETENTION_DAYS=%r — using 365", retention_raw)
        retention = 365

    return S3Config(
        endpoint_url=endpoint,
        region=_get("ALPTHERM_S3_REGION", "") or "",
        access_key=access_key,
        secret_key=secret_key,
        raw_bucket=raw_bucket,
        zarr_bucket=zarr_bucket,
        raw_retention_days=retention,
    )


def client(cfg: S3Config, max_pool_connections: int = 16):
    """A boto3 S3 client bound to the configured endpoint.

    Uses botocore's **adaptive** retry mode with a generous attempt budget so
    transient ``EndpointConnectionError``s — which Hetzner Object Storage
    returns when a bulk migration drives too many concurrent PUTs — are retried
    with client-side rate-limiting/backoff instead of surfacing as permanent
    failures. ``max_pool_connections`` is sized to cover the migration's worker
    threads so concurrent PUTs don't starve the connection pool.
    """
    import boto3
    from botocore.config import Config

    cfg_botocore = Config(
        retries={"max_attempts": 10, "mode": "adaptive"},
        max_pool_connections=max_pool_connections,
        connect_timeout=15,
        read_timeout=60,
    )
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region or None,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        config=cfg_botocore,
    )


def object_exists(s3, bucket: str, key: str) -> bool:
    """True iff ``key`` already exists in ``bucket`` (raw files are immutable)."""
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_raw(cfg: S3Config, local_path: Path, key: str, s3=None, check_exists=True) -> bool:
    """Upload one immutable raw file to the Object-Lock raw bucket.

    Governance-mode retention is applied automatically: the raw bucket carries
    a *default* retention rule (set once via :func:`ensure_raw_retention`), so
    every PUT inherits the WORM lock without a second round-trip — keeping the
    hot path and the bulk migration fast.

    Idempotent: skips the transfer if the key already exists (raw files are
    write-once). Pass ``check_exists=False`` when the caller has already
    diffed against a bucket listing, to avoid a redundant HEAD per file.

    Returns ``True`` if a new object was written, ``False`` if already present.
    """
    s3 = s3 or client(cfg)
    if check_exists and object_exists(s3, cfg.raw_bucket, key):
        return False
    s3.upload_file(str(local_path), cfg.raw_bucket, key)
    return True


def upload_zarr_tree(
    cfg: S3Config,
    local_zarr: Path,
    s3=None,
    workers: int = 6,
    prune_orphans: bool = True,
) -> dict[str, int]:
    """Mirror a local ``tier1.zarr`` directory to the zarr bucket via boto3.

    Used instead of an ``s3fs`` recursive ``put`` because boto3 auto-corrects
    request clock skew (it reads the server ``Date`` on a ``RequestTimeTooSkewed``
    response and re-signs), whereas ``s3fs``/``aiobotocore`` aborts the whole
    transfer on the first skewed chunk — which repeatedly stranded the multi-GB
    zarr put half-finished on a box with an intermittently jumping clock.

    Every file under ``local_zarr`` is uploaded (overwriting — chunks are
    mutable, unlike raw) to ``tier1.zarr/<relpath>`` on a thread pool. When
    ``prune_orphans`` is set, keys present in the bucket but no longer in the
    local store are deleted afterwards, so repeated/failed runs can't leave
    stale chunks behind that would corrupt the store.

    Returns a stats dict: ``uploaded``, ``failed``, ``pruned``.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s3 = s3 or client(cfg, max_pool_connections=max(workers, 10))
    prefix = ZARR_TIER1_PREFIX
    local_files = [p for p in local_zarr.rglob("*") if p.is_file()]
    desired = {f"{prefix}/{p.relative_to(local_zarr).as_posix()}": p for p in local_files}
    stats = {"uploaded": 0, "failed": 0, "pruned": 0}

    _tls = threading.local()

    def _put(item: tuple[str, Path]) -> None:
        key, path = item
        cl = getattr(_tls, "client", None)
        if cl is None:
            cl = _tls.client = client(cfg, max_pool_connections=max(workers, 10))
        cl.upload_file(str(path), cfg.zarr_bucket, key)

    log.info("zarr: uploading %d files → s3://%s/%s", len(desired), cfg.zarr_bucket, prefix)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_put, it): it[0] for it in desired.items()}
        for fut in as_completed(futures):
            try:
                fut.result()
                stats["uploaded"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                log.warning("zarr: upload failed for %s: %r", futures[fut], exc)

    if prune_orphans and not stats["failed"]:
        existing = set()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=cfg.zarr_bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents", []):
                existing.add(obj["Key"])
        orphans = sorted(existing - desired.keys())
        for i in range(0, len(orphans), 1000):
            batch = orphans[i : i + 1000]
            s3.delete_objects(
                Bucket=cfg.zarr_bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            stats["pruned"] += len(batch)
        if orphans:
            log.info("zarr: pruned %d orphan objects", stats["pruned"])
    elif prune_orphans and stats["failed"]:
        log.warning("zarr: skipping orphan prune because %d uploads failed", stats["failed"])

    log.info("zarr upload done: %s", stats)
    return stats


def ensure_raw_retention(cfg: S3Config, s3=None) -> None:
    """Set the raw bucket's *default* Governance retention rule (idempotent).

    Object Lock must already be enabled at bucket creation (it can't be
    retrofitted); this only installs the default retention *duration* so
    uploaded objects are WORM-protected without per-object calls. Safe to
    re-run — it just overwrites the rule with the configured day count.
    """
    s3 = s3 or client(cfg)
    if cfg.raw_retention_days <= 0:
        return
    s3.put_object_lock_configuration(
        Bucket=cfg.raw_bucket,
        ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {
                "DefaultRetention": {"Mode": "GOVERNANCE", "Days": cfg.raw_retention_days}
            },
        },
    )
