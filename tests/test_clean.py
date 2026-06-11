"""Tests for OGN track cleaning (igc_pipeline.clean)."""

from __future__ import annotations

import datetime as dt

from alptherm_icon.igc_pipeline.clean import clean_fixes

UTC = dt.timezone.utc


def _t(sec: float) -> dt.datetime:
    return dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC) + dt.timedelta(seconds=sec)


def test_multireceiver_dedup_to_one_per_second_median() -> None:
    # Same GPS-second heard by three receivers with slight jitter → one median fix.
    recs = [
        (_t(0.1), 47.00, 11.00, 1000.0),
        (_t(0.4), 47.02, 11.02, 1010.0),
        (_t(0.9), 47.01, 11.01, 1005.0),
        (_t(1.2), 47.10, 11.10, 1050.0),
    ]
    fixes = clean_fixes(recs)
    assert len(fixes) == 2  # two distinct seconds
    assert fixes[0].lat == 47.01 and fixes[0].lon == 11.01 and fixes[0].alt_m == 1005.0


def test_orders_by_gps_time() -> None:
    recs = [
        (_t(5), 47.0005, 11.0, 1000.0),
        (_t(1), 47.0001, 11.0, 1000.0),
        (_t(3), 47.0003, 11.0, 1000.0),
    ]
    fixes = clean_fixes(recs)
    assert [round(f.lat, 4) for f in fixes] == [47.0001, 47.0003, 47.0005]


def test_isolated_coordinate_jump_is_dropped() -> None:
    # A smooth slow track with one wild there-and-back spike in the middle.
    recs = [(_t(i), 47.0 + i * 1e-4, 11.0, 1000.0) for i in range(10)]
    recs[5] = (_t(5), 48.5, 12.5, 1000.0)  # ~150 km jump for 1 s → impossible
    fixes = clean_fixes(recs)
    assert all(f.lat < 47.5 for f in fixes)  # the spike is gone
    assert len(fixes) == 9


def test_clean_track_is_preserved() -> None:
    recs = [(_t(i), 47.0 + i * 1e-4, 11.0 + i * 1e-4, 1000.0 + i) for i in range(8)]
    fixes = clean_fixes(recs)
    assert len(fixes) == 8  # nothing dropped from a clean track
    assert [f.t for f in fixes] == sorted(f.t for f in fixes)
