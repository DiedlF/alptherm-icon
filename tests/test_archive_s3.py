"""Tests for the S3 archive backend config + key mapping (plan §9.6).

Pure logic — no network, no boto3 client construction.
"""

from __future__ import annotations

from pathlib import Path

from alptherm_icon.archive import s3
from alptherm_icon.archive.archiver import ArchiveRoot


def _write_env(tmp_path: Path, body: str) -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "archive.env").write_text(body, encoding="utf-8")
    return tmp_path


def test_load_s3_config_none_when_unconfigured(tmp_path: Path, monkeypatch) -> None:
    # No env file and no process env -> local-only mode.
    for k in (
        "ALPTHERM_S3_ENDPOINT",
        "ALPTHERM_S3_ACCESS_KEY",
        "ALPTHERM_S3_SECRET_KEY",
        "ALPTHERM_S3_RAW_BUCKET",
        "ALPTHERM_S3_ZARR_BUCKET",
    ):
        monkeypatch.delenv(k, raising=False)
    assert s3.load_s3_config(tmp_path) is None


def test_load_s3_config_none_when_partial(tmp_path: Path) -> None:
    # Missing the secret key -> still None, never a half-built config.
    _write_env(
        tmp_path,
        "ALPTHERM_S3_ENDPOINT=https://nbg1.your-objectstorage.com\n"
        "ALPTHERM_S3_ACCESS_KEY=AKIA\n"
        "ALPTHERM_S3_RAW_BUCKET=r\n"
        "ALPTHERM_S3_ZARR_BUCKET=z\n",
    )
    assert s3.load_s3_config(tmp_path) is None


def test_load_s3_config_full(tmp_path: Path) -> None:
    _write_env(
        tmp_path,
        "# comment line\n"
        "ALPTHERM_S3_ENDPOINT=https://nbg1.your-objectstorage.com\n"
        "ALPTHERM_S3_REGION=nbg1\n"
        "ALPTHERM_S3_ACCESS_KEY='AKIA'\n"
        'ALPTHERM_S3_SECRET_KEY="sek"\n'
        "ALPTHERM_S3_RAW_BUCKET=alptherm-raw\n"
        "ALPTHERM_S3_ZARR_BUCKET=alptherm-zarr\n"
        "ALPTHERM_S3_RAW_RETENTION_DAYS=30\n",
    )
    cfg = s3.load_s3_config(tmp_path)
    assert cfg is not None
    assert cfg.access_key == "AKIA"  # surrounding quotes stripped
    assert cfg.secret_key == "sek"
    assert cfg.raw_retention_days == 30
    assert cfg.zarr_url == "s3://alptherm-zarr/tier1.zarr"
    opts = cfg.storage_options
    assert opts["key"] == "AKIA"
    assert opts["client_kwargs"]["endpoint_url"].endswith("your-objectstorage.com")
    assert opts["client_kwargs"]["region_name"] == "nbg1"


def test_load_s3_config_bad_retention_falls_back(tmp_path: Path) -> None:
    _write_env(
        tmp_path,
        "ALPTHERM_S3_ENDPOINT=https://x\n"
        "ALPTHERM_S3_ACCESS_KEY=a\nALPTHERM_S3_SECRET_KEY=s\n"
        "ALPTHERM_S3_RAW_BUCKET=r\nALPTHERM_S3_ZARR_BUCKET=z\n"
        "ALPTHERM_S3_RAW_RETENTION_DAYS=not-a-number\n",
    )
    cfg = s3.load_s3_config(tmp_path)
    assert cfg is not None and cfg.raw_retention_days == 365


def test_raw_key_mirrors_grib_layout(tmp_path: Path) -> None:
    paths = ArchiveRoot(root=tmp_path)
    f = (
        paths.archive_dir
        / "grib" / "2026" / "05" / "31" / "06" / "tier2"
        / "icon-d2_germany_regular-lat-lon_model-level_2026053106_006_42_t.grib2"
    )
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    assert paths.raw_key(f) == (
        "grib/2026/05/31/06/tier2/"
        "icon-d2_germany_regular-lat-lon_model-level_2026053106_006_42_t.grib2"
    )
