"""Kreisflug-/Thermik-Detektion nach Richter 2011 (plan §6.2).

Findet Kreisflug-Phasen in einem :class:`Track` über die Krümmung
(akkumulierter Drehwinkel je gleitendem Zeitfenster) und extrahiert pro
Phase mittlere Vertikalgeschwindigkeit, mittlere Höhe und Zeitstempel.

Algorithmus:
1. Bearings je Segment, signierte Turns je Fix.
2. Rollendes Drehwinkel-Integral über ein ~25-s-Fenster (≈ ein
   Kreisumlauf). Ein Fix gilt als "kreisend", wenn |Integral| eine
   Schwelle überschreitet — d.h. innerhalb des Fensters wurde ein
   relevanter Bogen mit *konsistenter* Drehrichtung geflogen.
3. Aufeinanderfolgende kreisende Fixes werden zu Phasen gruppiert,
   kleine Lücken überbrückt.
4. Phasen-Filter (§6.2): Mindestdauer 2 min, ≥ 2 volle Umläufe,
   plausible Steigrate (Schlepp/Datenfehler-Ausschluss).

Drift-Korrektur (Verlagerungsvektor): für die Steigrate irrelevant
(dz/dt ist driftunabhängig); der Phasen-Centroid wird als Mittel der
Positionen genommen — Drift über 2 min ist klein gegen die
Regionsgröße. Eine echte Driftkorrektur ist ein späteres Refinement.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from alptherm_icon.igc_pipeline.track import (
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
    climb_rate_ms: float  # net (alt_top - alt_base) / duration
    n_turns: float  # |cumulative heading change| / 360
    turn_sign: int  # +1 clockwise, -1 counter-clockwise


@dataclass
class CirclingParams:
    window_s: float = 25.0  # rolling window ≈ one circle period
    min_turn_in_window_deg: float = 180.0  # ≥ half-circle in window = circling
    min_duration_s: float = 120.0  # §6.2 "Minimale Dauer 2 min"
    min_turns: float = 2.0  # at least two full circles
    max_climb_ms: float = 8.0  # above this = tow / data error, exclude
    max_gap_s: float = 20.0  # bridge short non-circling gaps within a phase


def _rolling_signed_turn(times_s: np.ndarray, turns: np.ndarray, window_s: float) -> np.ndarray:
    """For each fix i (aligned to the turn array), the signed sum of turns
    within the trailing ``window_s`` seconds. ``turns`` has length n-2,
    aligned to fixes[1:-1]; ``times_s`` is the full fix-time array.
    """
    # turn[k] is the turn at fix k+1 (between segment k and k+1).
    turn_times = times_s[1:-1]
    out = np.zeros_like(turns)
    j = 0
    csum = np.concatenate([[0.0], np.cumsum(turns)])
    for i in range(len(turns)):
        while turn_times[i] - turn_times[j] > window_s:
            j += 1
        out[i] = csum[i + 1] - csum[j]
    return out


def detect_thermals(track: Track, params: CirclingParams | None = None) -> list[ThermalPhase]:
    """Detect circling phases in a track. Returns one :class:`ThermalPhase`
    per qualifying phase."""
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

    # A fix is "circling" if the rolling signed-turn magnitude clears the
    # threshold — magnitude (not net) so a steady turn in one direction
    # qualifies while back-and-forth (ridge/S-turns) cancels out.
    circling = np.abs(rolling) >= p.min_turn_in_window_deg

    # Map circling flags (aligned to fixes[1:-1]) to phases by bridging
    # gaps shorter than max_gap_s.
    turn_times = times[1:-1]
    phases_idx: list[tuple[int, int]] = []
    start = None
    last_true = None
    for i, c in enumerate(circling):
        if c:
            if start is None:
                start = i
            last_true = i
        else:
            if start is not None and last_true is not None:
                if turn_times[i] - turn_times[last_true] > p.max_gap_s:
                    phases_idx.append((start, last_true))
                    start = None
                    last_true = None
    if start is not None and last_true is not None:
        phases_idx.append((start, last_true))

    results: list[ThermalPhase] = []
    for a, b in phases_idx:
        # Convert turn-array indices back to fix indices: turn k ↔ fix k+1.
        fa, fb = a + 1, b + 1
        t_start, t_end = track.fixes[fa].t, track.fixes[fb].t
        duration = (t_end - t_start).total_seconds()
        if duration < p.min_duration_s:
            continue
        cum_turn = float(np.sum(turns[a : b + 1]))
        n_turns = abs(cum_turn) / 360.0
        if n_turns < p.min_turns:
            continue
        alt_base = float(alts[fa])
        alt_top = float(alts[fb])
        climb = (alt_top - alt_base) / duration if duration > 0 else 0.0
        if abs(climb) > p.max_climb_ms:
            continue  # tow / data spike
        results.append(
            ThermalPhase(
                source_id=track.source_id,
                aircraft_type=track.aircraft_type,
                t_start=t_start,
                t_end=t_end,
                duration_s=duration,
                lat_centroid=float(np.mean(lats[fa : fb + 1])),
                lon_centroid=float(np.mean(lons[fa : fb + 1])),
                alt_mean_m=float(np.mean(alts[fa : fb + 1])),
                alt_base_m=alt_base,
                alt_top_m=alt_top,
                climb_rate_ms=climb,
                n_turns=n_turns,
                turn_sign=1 if cum_turn >= 0 else -1,
            )
        )
    return results
