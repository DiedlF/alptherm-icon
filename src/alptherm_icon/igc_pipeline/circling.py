"""Kreisflug-/Thermik-Detektion nach Richter 2011 (plan §6.2).

Two complementary circling signatures, OR-combined so OGN reception artifacts
don't hide a thermal:

1. **Heading integral** — a rolling signed-turn sum over ~25 s. Detects circling
   when the heading rotates consistently. Accurate turn counts, but needs a
   well-sampled track (≳ 4 fixes per circle).

2. **Spatial confinement + climb** — the fixes stay within a small radius of
   their local centroid over a longer window while altitude rises. This catches
   the OGN failure mode the heading method misses: *directional / sparse
   reception* (terrain shadow, antenna pattern), where only an arc of each circle
   is received or the track is undersampled (a paraglider circling every ~18 s
   heard every ~10 s) — the heading integral then aliases into noise, but the
   aircraft still climbs in a confined footprint, which is unambiguous.

For undersampled (confinement-only) phases the heading-derived turn count is
meaningless, so ``n_turns`` is estimated from duration ÷ a nominal circle period
and the phase is tagged ``method="confined"`` (vs ``"turn"`` for measured ones).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from alptherm_icon.igc_pipeline.track import (
    EARTH_R_M,
    Track,
    bearings_deg,
    signed_turns_deg,
)


@dataclass
class ThermalPhase:
    """One detected circling phase."""

    source_id: str
    aircraft_type: int | None
    t_start: dt.datetime
    t_end: dt.datetime
    duration_s: float
    lat_centroid: float
    lon_centroid: float
    alt_mean_m: float
    alt_base_m: float  # altitude at phase start
    alt_top_m: float  # altitude at phase end
    climb_rate_ms: float  # robust linear-fit climb over the phase
    n_turns: float  # measured (method="turn") or estimated (method="confined")
    turn_sign: int  # +1 clockwise, -1 counter-clockwise
    method: str  # "turn" (heading integral) or "confined" (confinement + climb)


@dataclass
class CirclingParams:
    window_s: float = 25.0  # rolling window ≈ one circle period
    min_turn_in_window_deg: float = 180.0  # ≥ half-circle in window = circling
    min_duration_s: float = 120.0  # §6.2 "Minimale Dauer 2 min"
    min_turns: float = 2.0  # at least two full circles (measured path)
    max_climb_ms: float = 8.0  # above this = tow / data error, exclude
    max_gap_s: float = 20.0  # bridge short non-circling gaps within a phase
    # Confinement path (catches directional / undersampled reception):
    confine_window_s: float = 120.0  # longer window — may span several circles
    max_radius_m: float = 400.0  # thermal footprint; cruising leaves this fast
    min_confine_pts: int = 6
    min_confine_gain_m: float = 80.0  # net climb required for a confinement-only phase
    dense_dt_s: float = 7.0  # median spacing below this → turn count is trustworthy
    nominal_circle_s: float = 20.0  # circle period for estimating undersampled turns


def _rolling_signed_turn(times_s: np.ndarray, turns: np.ndarray, window_s: float) -> np.ndarray:
    """For each fix i (aligned to the turn array), the signed sum of turns
    within the trailing ``window_s`` seconds. ``turns`` has length n-2,
    aligned to fixes[1:-1]; ``times_s`` is the full fix-time array.
    """
    turn_times = times_s[1:-1]
    out = np.zeros_like(turns)
    j = 0
    csum = np.concatenate([[0.0], np.cumsum(turns)])
    for i in range(len(turns)):
        while turn_times[i] - turn_times[j] > window_s:
            j += 1
        out[i] = csum[i + 1] - csum[j]
    return out


def _rolling_confined(
    times_s: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    window_s: float,
    max_radius_m: float,
    min_pts: int,
) -> np.ndarray:
    """Per-fix flag: are the points in the trailing ``window_s`` confined within
    ``max_radius_m`` of their centroid? (Equirectangular metres — fine at thermal
    scale.) A climbing aircraft confined to a thermal footprint trips this even
    when its rotation wasn't fully received."""
    n = times_s.size
    lat0 = np.radians(float(np.mean(lats)))
    x = EARTH_R_M * np.radians(lons) * np.cos(lat0)
    y = EARTH_R_M * np.radians(lats)
    confined = np.zeros(n, dtype=bool)
    j = 0
    for i in range(n):
        while times_s[i] - times_s[j] > window_s:
            j += 1
        if i - j + 1 < min_pts:
            continue
        xs, ys = x[j : i + 1], y[j : i + 1]
        r = np.sqrt((xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2).max()
        confined[i] = r <= max_radius_m
    return confined


def _phase_radius_m(lats: np.ndarray, lons: np.ndarray) -> float:
    lat0 = np.radians(float(np.mean(lats)))
    x = EARTH_R_M * np.radians(lons) * np.cos(lat0)
    y = EARTH_R_M * np.radians(lats)
    return float(np.sqrt((x - x.mean()) ** 2 + (y - y.mean()) ** 2).max())


def detect_thermals(track: Track, params: CirclingParams | None = None) -> list[ThermalPhase]:
    """Detect circling phases in a track."""
    p = params or CirclingParams()
    if len(track) < 5:
        return []
    track = track.sorted()
    times = track.times_s
    lats, lons, alts = track.lats, track.lons, track.alts

    bear = bearings_deg(lats, lons)
    if bear.size < 3:
        return []
    turns = signed_turns_deg(bear)  # length n-2, aligned to fixes[1:-1]
    rolling = _rolling_signed_turn(times, turns, p.window_s)
    turn_flag = np.abs(rolling) >= p.min_turn_in_window_deg

    confined = _rolling_confined(
        times, lats, lons, p.confine_window_s, p.max_radius_m, p.min_confine_pts
    )
    # Align confinement (per-fix) to the turn-array index space (turn k ↔ fix k+1).
    confined_aligned = confined[1:-1]
    circling = turn_flag | confined_aligned

    turn_times = times[1:-1]
    phases_idx: list[tuple[int, int]] = []
    start = last_true = None
    for i, c in enumerate(circling):
        if c:
            start = i if start is None else start
            last_true = i
        elif start is not None and last_true is not None:
            if turn_times[i] - turn_times[last_true] > p.max_gap_s:
                phases_idx.append((start, last_true))
                start = last_true = None
    if start is not None and last_true is not None:
        phases_idx.append((start, last_true))

    results: list[ThermalPhase] = []
    for a, b in phases_idx:
        fa, fb = a + 1, b + 1
        t_start, t_end = track.fixes[fa].t, track.fixes[fb].t
        duration = (t_end - t_start).total_seconds()
        if duration < p.min_duration_s:
            continue

        tw, aw = times[fa : fb + 1], alts[fa : fb + 1]
        if tw.size >= 2 and (tw[-1] - tw[0]) > 0:
            climb = float(np.polyfit(tw - tw[0], aw, 1)[0])
        else:
            climb = (float(aw[-1]) - float(aw[0])) / duration if duration > 0 else 0.0
        if abs(climb) > p.max_climb_ms:
            continue  # tow / data spike

        cum_turn = float(np.sum(turns[a : b + 1]))
        n_turns_meas = abs(cum_turn) / 360.0
        median_dt = float(np.median(np.diff(tw))) if tw.size > 1 else duration
        dense = median_dt <= p.dense_dt_s
        radius = _phase_radius_m(lats[fa : fb + 1], lons[fa : fb + 1])
        net_gain = float(aw[-1] - aw[0])

        is_turn = dense and n_turns_meas >= p.min_turns
        is_confined = radius <= p.max_radius_m and net_gain >= p.min_confine_gain_m
        if not (is_turn or is_confined):
            continue

        if dense:
            n_turns = n_turns_meas
            method = "turn" if is_turn else "confined"
        else:
            n_turns = duration / p.nominal_circle_s  # heading aliased → estimate
            method = "confined"

        results.append(
            ThermalPhase(
                source_id=track.source_id,
                aircraft_type=track.aircraft_type,
                t_start=t_start,
                t_end=t_end,
                duration_s=duration,
                lat_centroid=float(np.median(lats[fa : fb + 1])),
                lon_centroid=float(np.median(lons[fa : fb + 1])),
                alt_mean_m=float(np.mean(aw)),
                alt_base_m=float(aw[0]),
                alt_top_m=float(aw[-1]),
                climb_rate_ms=climb,
                n_turns=n_turns,
                turn_sign=1 if cum_turn >= 0 else -1,
                method=method,
            )
        )
    return results
