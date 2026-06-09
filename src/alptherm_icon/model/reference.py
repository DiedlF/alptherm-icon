"""Liechti & Neininger (1994) reference inputs — validation harness only.

Loads the worked-example morning sounding (Table 1 / Fig 2) used to reproduce
Table 2 and Figure 4. This is **not** part of the operational ICON path; it
exists so the parcel kernel can be checked against the paper's own inputs.

Fig-2 superposition (as described in the fixture header): the ground-station
network defines the profile within the topography, the radiosonde defines the
free atmosphere aloft, and the radiosonde weight grows with height. The exact
weighting curve is not recoverable from the scanned table, so we use a
documented approximation: a height-blend that ramps from station-dominated at
the surface to radiosonde-dominated above the topography band, modulated by the
per-station weight column. Emergent behaviour (cloud onset, base) is what the
golden test checks, with tolerances reflecting this.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from alptherm_icon.model.thermo import potential_temperature, standard_pressure

# Top of the topography blend band [m ASL]: below this the ground-station
# network dominates; the radiosonde weight ramps to full above it. The Alpine
# crest stations in Table 1 top out near here.
TOPO_BLEND_TOP_M = 2300.0
BIN_HEIGHT_M = 100.0


@dataclass
class InitialSounding:
    """Morning sounding on a regular height grid (ascending z, m ASL)."""

    z_m: np.ndarray
    T_K: np.ndarray
    Td_K: np.ndarray
    theta_K: np.ndarray
    p_Pa: np.ndarray


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(line for line in fh if not line.startswith("#"))
        for row in reader:
            rows.append(row)
    return rows


def _interp_source(
    rows: list[dict[str, str]], source: str, z_grid: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Interpolate one source's T/Td (°C) onto z_grid; return T, Td [K] and a
    validity mask (False where the grid height is outside the source's range)."""
    pts = sorted(
        ((float(r["height_m"]), float(r["T_C"]), float(r["Td_C"])) for r in rows if r["source"] == source),
        key=lambda t: t[0],
    )
    z = np.array([p[0] for p in pts])
    t_c = np.array([p[1] for p in pts])
    td_c = np.array([p[2] for p in pts])
    in_range = (z_grid >= z[0]) & (z_grid <= z[-1])
    T = np.interp(z_grid, z, t_c) + 273.15
    Td = np.interp(z_grid, z, td_c) + 273.15
    return T, Td, in_range


def load_initial_sounding(
    csv_path: str | Path,
    z_top_m: float = 5000.0,
    bin_height_m: float = BIN_HEIGHT_M,
) -> InitialSounding:
    """Load and superpose the 1994 worked-example sounding onto a 100 m grid."""
    rows = _read_rows(Path(csv_path))
    heights = [float(r["height_m"]) for r in rows]
    z_grid = np.arange(
        np.floor(min(heights) / bin_height_m) * bin_height_m,
        z_top_m + bin_height_m,
        bin_height_m,
    )

    T_st, Td_st, st_ok = _interp_source(rows, "station_08h", z_grid)
    T_rs, Td_rs, rs_ok = _interp_source(rows, "radiosonde_02h", z_grid)

    # Radiosonde weight ramps 0→1 across the topography band; station weight is
    # the complement. Outside a source's range its weight drops to 0.
    w_rs = np.clip(z_grid / TOPO_BLEND_TOP_M, 0.0, 1.0) * rs_ok
    w_st = (1.0 - np.clip(z_grid / TOPO_BLEND_TOP_M, 0.0, 1.0)) * st_ok
    # Fall back to whichever source is valid where the other is missing.
    w_rs = np.where((w_rs + w_st) == 0.0, rs_ok.astype(float), w_rs)
    w_st = np.where((w_rs + w_st) == 0.0, st_ok.astype(float), w_st)
    w_sum = w_rs + w_st

    T = (w_st * T_st + w_rs * T_rs) / w_sum
    Td = (w_st * Td_st + w_rs * Td_rs) / w_sum
    Td = np.minimum(Td, T)  # dewpoint cannot exceed temperature

    p = np.asarray(standard_pressure(z_grid), dtype=np.float64)
    theta = np.asarray(potential_temperature(T, p), dtype=np.float64)
    return InitialSounding(z_m=z_grid, T_K=T, Td_K=Td, theta_K=theta, p_Pa=p)
