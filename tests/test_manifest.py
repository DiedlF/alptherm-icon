"""Tests for the manifest recovery semantics (plan §9.3).

Covers the distinction between *any* record and a *successful* record — the
basis for backfill being able to retry a previously-failed init.
"""

from __future__ import annotations

from pathlib import Path

from alptherm_icon.archive import manifest
from alptherm_icon.archive.manifest import ManifestRecord


def _record(init_utc: str, files_ok: int, files_404: int = 0) -> ManifestRecord:
    return ManifestRecord(
        init_utc=init_utc,
        tier="tier1",
        finished_utc="2026-06-08T12:00:00Z",
        files_attempted=files_ok + files_404,
        files_ok=files_ok,
        files_404=files_404,
        files_error=0,
        bytes_on_disk=files_ok * 1000,
        variables=["t_2m"],
    )


def test_failed_record_is_not_successful(tmp_path: Path) -> None:
    """An all-404 attempt leaves a record but stays retryable."""
    path = tmp_path / "manifest.jsonl"
    init = "2026-06-07T09:00:00Z"
    manifest.append(_record(init, files_ok=0, files_404=833), path)

    # The failed attempt is visible as *a* record...
    assert manifest.has_record(path, init, "tier1") is True
    # ...but not as a successful one, so archive_tier1 will retry it.
    assert manifest.has_successful_record(path, init, "tier1") is False


def test_successful_record_blocks_retry(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    init = "2026-06-08T09:00:00Z"
    manifest.append(_record(init, files_ok=833), path)
    assert manifest.has_successful_record(path, init, "tier1") is True


def test_later_success_supersedes_earlier_failure(tmp_path: Path) -> None:
    """A failed attempt followed by a successful retry counts as successful."""
    path = tmp_path / "manifest.jsonl"
    init = "2026-06-08T00:00:00Z"
    manifest.append(_record(init, files_ok=0, files_404=833), path)  # broken run
    manifest.append(_record(init, files_ok=833), path)  # backfill recovery
    assert manifest.has_successful_record(path, init, "tier1") is True


def test_no_record_for_unseen_init(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    assert manifest.has_successful_record(path, "2026-06-05T03:00:00Z", "tier1") is False
