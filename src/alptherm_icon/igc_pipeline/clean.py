"""OGN track cleaning — derived analysis layer (plan §6.2 / §9.5).

Raw OGN logs carry the *same* aircraft position relayed by many ground stations
(≈28 receivers for one glider), with beacons ordered by **receive** time rather
than GPS time (~1 in 4 consecutive steps goes backwards in real time) and the
occasional bad relay fix. Fed straight into circle detection that produces the
back-and-forth artifacts, inflated turn counts and off-centre thermals seen on
OGN data (IGC/WeGlide is a single clean recorder track and needs none of this).

:func:`clean_fixes` turns raw per-aircraft observations into a clean track:

1. **GPS-time order** — caller supplies the GPS packet time, not ``ts_recv``;
2. **receiver-consensus dedup** — one fix per GPS-second, median lat/lon/alt
   across the receivers that heard it (robust to a single bad relay);
3. **jump rejection** — drop isolated fixes implying an impossible ground speed
   or climb rate relative to *both* neighbours (a there-and-back spike).

The raw layer is never touched (it stays the immutable, S3-WORM ground truth);
this is fully regenerable from it.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from alptherm_icon.igc_pipeline.track import Fix, haversine_m

MAX_GROUND_SPEED_MS = 70.0  # ~250 km/h — a soaring aircraft above this is a glitch
MAX_CLIMB_RATE_MS = 15.0  # vertical-speed gate for jump rejection


def _is_spike(prev: Fix, cur: tuple, nxt: tuple, max_speed: float, max_climb: float) -> bool:
    """True if ``cur`` is an isolated there-and-back jump vs both neighbours."""
    def step(a_t, a_lat, a_lon, a_alt, b_t, b_lat, b_lon, b_alt) -> tuple[float, float]:
        dts = abs((b_t - a_t).total_seconds()) or 1.0
        return haversine_m(a_lat, a_lon, b_lat, b_lon) / dts, abs(b_alt - a_alt) / dts
    s1, c1 = step(prev.t, prev.lat, prev.lon, prev.alt_m, *cur)
    s2, c2 = step(*cur, nxt[0], nxt[1], nxt[2], nxt[3])
    return (s1 > max_speed and s2 > max_speed) or (c1 > max_climb and c2 > max_climb)


def clean_fixes(
    records,
    max_speed_ms: float = MAX_GROUND_SPEED_MS,
    max_climb_ms: float = MAX_CLIMB_RATE_MS,
) -> list[Fix]:
    """Clean raw OGN observations into a GPS-time-ordered, deduped track.

    ``records``: iterable of ``(gps_dt, lat, lon, alt)`` — one per raw position
    beacon (the same fix may appear several times, once per receiver).
    """
    by_sec: dict[dt.datetime, list[tuple[float, float, float]]] = {}
    for gps_dt, lat, lon, alt in records:
        sec = gps_dt.replace(microsecond=0)
        by_sec.setdefault(sec, []).append((float(lat), float(lon), float(alt)))

    merged: list[tuple[dt.datetime, float, float, float]] = []
    for sec, obs in sorted(by_sec.items()):
        arr = np.asarray(obs, dtype=float)
        merged.append((sec, float(np.median(arr[:, 0])), float(np.median(arr[:, 1])), float(np.median(arr[:, 2]))))

    if len(merged) < 3:
        return [Fix(t=s, lat=la, lon=lo, alt_m=al) for s, la, lo, al in merged]

    # Jump rejection: drop isolated there-and-back spikes (two passes is enough).
    for _ in range(2):
        kept = [merged[0]]
        for i in range(1, len(merged) - 1):
            prev = Fix(t=kept[-1][0], lat=kept[-1][1], lon=kept[-1][2], alt_m=kept[-1][3])
            if _is_spike(prev, merged[i], merged[i + 1], max_speed_ms, max_climb_ms):
                continue
            kept.append(merged[i])
        kept.append(merged[-1])
        if len(kept) == len(merged):
            break
        merged = kept

    return [Fix(t=s, lat=la, lon=lo, alt_m=al) for s, la, lo, al in merged]
