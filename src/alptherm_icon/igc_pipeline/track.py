"""Flight-track primitives + geometry for Komp. D (plan §6.2).

Source-agnostic: a :class:`Track` is just a time-ordered sequence of
:class:`Fix` (time, lat, lon, altitude). The same type carries an
OGN-assembled track and a parsed IGC file, so the circling detector
in :mod:`circling` runs unchanged on both.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

EARTH_R_M = 6_371_000.0


@dataclass(frozen=True)
class Fix:
    """One position report."""

    t: dt.datetime
    lat: float
    lon: float
    alt_m: float


# OGN/FLARM aircraft-type codes relevant to soaring.
AIRCRAFT_TYPE_LABEL = {
    1: "glider",
    6: "hangglider",
    7: "paraglider",
}
SOARING_AIRCRAFT_TYPES = frozenset(AIRCRAFT_TYPE_LABEL)
"""Glider / hang-glider / paraglider. Excludes powered (8), jet (9),
heli (3), balloon (11) etc. — the ADS-B/SafeSky traffic that otherwise
pollutes thermal stats with holding patterns at FL300."""


@dataclass
class Track:
    """Time-ordered fixes for one aircraft. ``source_id`` is the OGN
    sender ID or IGC filename — opaque to the detector. ``aircraft_type``
    is the OGN code (1=glider, 6=hangglider, 7=paraglider) so the
    downstream stats can split paraglider vs. sailplane (plan §5.5)."""

    source_id: str
    fixes: list[Fix]
    aircraft_type: int | None = None

    def __len__(self) -> int:
        return len(self.fixes)

    def sorted(self) -> "Track":
        return Track(
            self.source_id,
            sorted(self.fixes, key=lambda f: f.t),
            aircraft_type=self.aircraft_type,
        )

    # --- vectorised views (cached lazily would be premature; cheap enough) ---
    @property
    def times_s(self) -> np.ndarray:
        t0 = self.fixes[0].t
        return np.array([(f.t - t0).total_seconds() for f in self.fixes], dtype=float)

    @property
    def lats(self) -> np.ndarray:
        return np.array([f.lat for f in self.fixes], dtype=float)

    @property
    def lons(self) -> np.ndarray:
        return np.array([f.lon for f in self.fixes], dtype=float)

    @property
    def alts(self) -> np.ndarray:
        return np.array([f.alt_m for f in self.fixes], dtype=float)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def bearings_deg(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Initial bearing (deg, 0=N, clockwise) for each consecutive segment.

    Returns an array of length n-1 for n fixes.
    """
    lat1 = np.radians(lats[:-1])
    lat2 = np.radians(lats[1:])
    dlon = np.radians(lons[1:] - lons[:-1])
    x = np.sin(dlon) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return (np.degrees(np.arctan2(x, y)) + 360.0) % 360.0


def wrap180(deg: np.ndarray) -> np.ndarray:
    """Wrap angle differences to (-180, 180]."""
    return (deg + 180.0) % 360.0 - 180.0


def signed_turns_deg(bearings: np.ndarray) -> np.ndarray:
    """Signed turn between consecutive bearings (deg). Length n-2 for n
    fixes. Positive = clockwise (right turn)."""
    return wrap180(np.diff(bearings))
