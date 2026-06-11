"""Clean-track cache — the derived, regenerable hot layer (plan §9.5).

Parsing a day's raw OGN log (~25 M beacons, ~18 min for the busiest day) just to
re-run circle detection is wasteful when iterating on detector parameters. This
caches the *cleaned* per-aircraft fixes (after GPS-time ordering, receiver-median
dedup and jump rejection — see :mod:`clean`) as one parquet per day under
``data/ogn/clean/``. Detection and the dashboard read that in seconds; the raw
layer stays the immutable ground truth and the cache is rebuildable from it.

Columns: ``source_id, aircraft_type, t, lat, lon, alt_m`` (one row per clean fix).
The cache holds the standard convective window (assembled with a low min-fixes);
consumers apply their own min-fixes filter on read.
"""

from __future__ import annotations

from pathlib import Path

from alptherm_icon.igc_pipeline.track import Fix, Track


def clean_cache_path(root: Path, day: str) -> Path:
    return root / "data" / "ogn" / "clean" / f"{day}_clean.parquet"


def write_clean_parquet(tracks: list[Track], path: Path) -> int:
    """Flatten cleaned tracks to one row per fix and write a parquet. Returns rows."""
    import pandas as pd

    rows = [
        (tr.source_id, tr.aircraft_type, f.t, f.lat, f.lon, f.alt_m)
        for tr in tracks
        for f in tr.fixes
    ]
    df = pd.DataFrame(rows, columns=["source_id", "aircraft_type", "t", "lat", "lon", "alt_m"])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return len(df)


def read_clean_parquet(path: Path, source_id: str | None = None) -> list[Track]:
    """Read cached clean tracks. If ``source_id`` is given, only that aircraft
    (predicate pushed down to parquet, so it stays fast on big days)."""
    import pandas as pd

    filters = [("source_id", "==", source_id)] if source_id else None
    df = pd.read_parquet(path, filters=filters)
    tracks: list[Track] = []
    for sid, g in df.groupby("source_id", sort=False):
        g = g.sort_values("t")
        fixes = [
            Fix(
                t=t.to_pydatetime() if hasattr(t, "to_pydatetime") else t,
                lat=float(la),
                lon=float(lo),
                alt_m=float(al),
            )
            for t, la, lo, al in zip(g["t"], g["lat"], g["lon"], g["alt_m"])
        ]
        ac = g["aircraft_type"].iloc[0]
        tracks.append(
            Track(source_id=str(sid), fixes=fixes, aircraft_type=None if pd.isna(ac) else int(ac))
        )
    return tracks
