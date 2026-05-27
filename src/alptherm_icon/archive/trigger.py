"""Tier 2 gut-day trigger (plan §9.2, §9.3).

Diagnostic, not predictive. Per §9.2, the decision is made *after*
convection has started: at ~12 UTC we evaluate the 09-UTC-Lauf
(available ~11:30 UTC) at small leads — that run already "sees" the
early afternoon ~2 h ahead, essentially diagnostically. If the
decision fires, the Tier-2 Vollprofil of the *06-UTC-Lauf* (still in
the DWD ~48 h window) is queued for the nightly bulk download.

This decouples the decision input (09 UTC, current) from the
archived run (06 UTC, best Flugtag coverage).

OR-gate semantics (per design Q3): we want to over-capture rather
than miss. But the gate is *areal*, not spatial-max — a single active
cell in the 270 000 km² Bbox no longer fires the trigger. Each branch
asks "is this widespread?" via a per-cell threshold + an areal-coverage
fraction. Tier 2 fires if any of:

- ``cape_ml`` exceeds CAPE_CELL_THRESH on at least CAPE_COVER_FRAC of
  the Bbox cells (on any lead in the convective window) — widespread
  convective potential, not an isolated cell;
- ``asob_s`` exceeds RAD_CELL_THRESH on at least RAD_COVER_FRAC — a
  broadly sunny day (catches Blue Days with low CAPE but strong
  forcing);
- the day is *dry* (wet-cell fraction below PRECIP_WET_MAX_FRAC) AND
  ``htop_dc`` reaches HTOP_DC_CELL_MIN_M on at least HTOP_DC_COVER_FRAC
  — i.e. widespread dry convection, the textbook Blue-Day pattern §8
  calls most under-represented in IGC.

Why areal instead of spatial-max (the v1 design): empirically every
summer day had CAPE > 100 J/kg *somewhere* in the Alps, so the old
spatial-max gate fired 100 % of the time and lost all selectivity
(§9.1/§9.2 wanted *good* days, not *every* day). Coverage fractions
make the gate represent the whole Bbox, and the per-cell thresholds are
recalibrated upward to "kräftige Thermik" levels.

The decision logs the evaluated coverage fractions so we can audit /
re-tune thresholds later from the manifest alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging

import numpy as np
import xarray as xr

from alptherm_icon.archive.bbox import ALPEN_BBOX, BBox, subset_to_bbox

log = logging.getLogger(__name__)

# Convective window in lead-hours from the 09-UTC decision run.
# Leads 1..6 = 10..15 UTC = 12..17 local in summer — the diagnostic
# afternoon-peak window we evaluate against (cape_ml, asob_s, etc.).
TRIGGER_LEAD_RANGE: tuple[int, int] = (1, 6)

# --- Areal-coverage gate (per-cell threshold + min coverage fraction) ---
# Each pair: a cell is "active" above the *_CELL_* threshold; the branch
# fires if the active fraction of the Bbox exceeds the *_COVER_FRAC*.
# Coverage is the max over the leads in the window. All to be re-tuned
# against IGC/OGN overlap once a season is archived.

CAPE_CELL_THRESH = 500.0  # J/kg per cell — "kräftige Thermik" (was max>100)
CAPE_COVER_FRAC = 0.10  # ≥10 % of the Bbox convectively active = widespread

RAD_CELL_THRESH = 600.0  # W/m² per cell — strong insolation (avg since init)
RAD_COVER_FRAC = 0.25  # ≥25 % broadly sunny = good radiation day

# Blue-Day branch — both conditions areal:
PRECIP_WET_CELL_MM = 1.0  # a cell is "wet" if window precip exceeds this
PRECIP_WET_MAX_FRAC = 0.10  # day counts as dry if <10 % of cells are wet
HTOP_DC_CELL_MIN_M = 2500.0  # cell has real dry convection above this [m MSL]
HTOP_DC_COVER_FRAC = 0.20  # ≥20 % of cells dry-convective


@dataclass(frozen=True)
class TriggerDecision:
    fire: bool
    reason: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"fire": self.fire, "reason": self.reason, "metrics": self.metrics}


def _open_grib(path: Path, step_h: int) -> xr.Dataset | None:
    """Open a GRIB2 and collapse multi-step files to one full-hour slice.

    cape_ml / tot_prec / hbas_sc / htop_sc are published by DWD with
    15-minute sub-steps packed into the hourly file. Filtering on
    ``step`` = ``step_h`` (in hours) selects the full-hour message and
    keeps spatial statistics meaningful (otherwise ``arr.max()`` runs
    over 4 stacked frames including the longest accumulation slice).

    Returns ``None`` on read errors so one corrupt file doesn't take
    down the trigger pass.
    """
    try:
        return xr.open_dataset(
            path,
            engine="cfgrib",
            backend_kwargs={
                "indexpath": "",
                "filter_by_keys": {"step": float(step_h)},
            },
        )
    except Exception as exc:  # noqa: BLE001 — cfgrib raises EOFError, ValueError, etc.
        log.warning("trigger: failed to open %s: %r", path.name, exc)
        return None


def _bbox_values(ds: xr.Dataset) -> np.ndarray:
    """Flat array of finite Bbox cell values for the dataset's single var."""
    sub = subset_to_bbox(ds, ALPEN_BBOX)
    da = sub[next(iter(sub.data_vars))]
    arr = np.asarray(da.values, dtype=np.float64).ravel()
    return arr[np.isfinite(arr)]


def _bbox_coverage_above(ds: xr.Dataset, threshold: float) -> float:
    """Fraction of finite Bbox cells whose value exceeds ``threshold``."""
    arr = _bbox_values(ds)
    if arr.size == 0:
        return float("nan")
    return float((arr > threshold).mean())


def _bbox_spatial_stat(ds: xr.Dataset, stat: str = "max") -> float:
    """Spatial statistic over the Alpen-Bbox. Kept for the metrics
    payload (we still log the spatial-max for human reference)."""
    arr = _bbox_values(ds)
    if arr.size == 0:
        return float("nan")
    if stat == "max":
        return float(arr.max())
    if stat == "mean":
        return float(arr.mean())
    raise ValueError(f"unknown stat {stat!r}")


def _finite_max(xs: list[float]) -> float:
    finite = [v for v in xs if np.isfinite(v)]
    return max(finite) if finite else float("nan")


def evaluate(
    grib_paths: dict[tuple[str, int], Path],
    lead_range: tuple[int, int] = TRIGGER_LEAD_RANGE,
    bbox: BBox = ALPEN_BBOX,
) -> TriggerDecision:
    """Decide Tier 2 from already-downloaded Tier 1 GRIB2 paths.

    Areal-coverage gate (see module docstring). For ``cape_ml`` /
    ``asob_s`` / ``htop_dc`` we take, per lead, the fraction of Bbox
    cells above the per-cell threshold, then the max coverage over the
    window. ``tot_prec`` is handled per-cell over the accumulation
    window to get the wet-cell fraction.

    Parameters
    ----------
    grib_paths
        Mapping ``(var, lead_h) -> Path`` covering at minimum ``cape_ml``,
        ``asob_s``, ``tot_prec``, ``htop_dc`` for the leads in
        ``lead_range``. Missing paths are tolerated; they just don't
        contribute to their axis of the OR-gate.
    """
    lead_lo, lead_hi = lead_range
    leads = range(lead_lo, lead_hi + 1)

    cape_cov_per_lead: list[float] = []
    rad_cov_per_lead: list[float] = []
    htop_dc_cov_per_lead: list[float] = []
    cape_max_per_lead: list[float] = []  # logged for reference only

    cell_thresh = {
        "cape_ml": CAPE_CELL_THRESH,
        "asob_s": RAD_CELL_THRESH,
        "htop_dc": HTOP_DC_CELL_MIN_M,
    }
    cov_target = {
        "cape_ml": cape_cov_per_lead,
        "asob_s": rad_cov_per_lead,
        "htop_dc": htop_dc_cov_per_lead,
    }

    for lead in leads:
        for var in ("cape_ml", "asob_s", "htop_dc"):
            path = grib_paths.get((var, lead))
            if path is None or not path.exists():
                continue
            ds = _open_grib(path, step_h=lead)
            if ds is None:  # corrupt / truncated GRIB — skip, don't crash
                continue
            try:
                cov_target[var].append(_bbox_coverage_above(ds, cell_thresh[var]))
                if var == "cape_ml":
                    cape_max_per_lead.append(_bbox_spatial_stat(ds, "max"))
            finally:
                ds.close()

    # TOT_PREC wet-cell fraction over the accumulation window, per cell:
    # window = tot_prec[lead_hi] - tot_prec[lead_lo-1]. At lead 0 nothing
    # has fallen, so the lower bound is an implicit zero field.
    def _accum_array(lead: int) -> np.ndarray | None:
        if lead == 0:
            return None  # implicit zero field
        p = grib_paths.get(("tot_prec", lead))
        if p is None or not p.exists():
            return None
        ds = _open_grib(p, step_h=lead)
        if ds is None:
            return None
        try:
            return _bbox_values(ds)
        finally:
            ds.close()

    wet_frac = float("nan")
    hi_arr = _accum_array(lead_hi)
    lo_arr = _accum_array(max(lead_lo - 1, 0))
    if hi_arr is not None and hi_arr.size:
        if lo_arr is not None and lo_arr.shape == hi_arr.shape:
            window = hi_arr - lo_arr
        else:
            window = hi_arr  # lower bound is the zero field
        wet_frac = float((window > PRECIP_WET_CELL_MM).mean())

    cape_cover = _finite_max(cape_cov_per_lead)
    rad_cover = _finite_max(rad_cov_per_lead)
    htop_dc_cover = _finite_max(htop_dc_cov_per_lead)
    cape_max = _finite_max(cape_max_per_lead)

    reasons: list[str] = []
    if np.isfinite(cape_cover) and cape_cover > CAPE_COVER_FRAC:
        reasons.append(
            f"cape_cover={cape_cover:.0%}>{CAPE_COVER_FRAC:.0%}"
            f"@{CAPE_CELL_THRESH:.0f}"
        )
    if np.isfinite(rad_cover) and rad_cover > RAD_COVER_FRAC:
        reasons.append(
            f"rad_cover={rad_cover:.0%}>{RAD_COVER_FRAC:.0%}@{RAD_CELL_THRESH:.0f}"
        )
    if (
        np.isfinite(wet_frac)
        and wet_frac < PRECIP_WET_MAX_FRAC
        and np.isfinite(htop_dc_cover)
        and htop_dc_cover > HTOP_DC_COVER_FRAC
    ):
        reasons.append(
            f"blue_day(wet={wet_frac:.0%}<{PRECIP_WET_MAX_FRAC:.0%},"
            f"htop_dc_cover={htop_dc_cover:.0%}>{HTOP_DC_COVER_FRAC:.0%})"
        )

    fire = bool(reasons)
    return TriggerDecision(
        fire=fire,
        reason=" OR ".join(reasons) if reasons else "no_threshold_met",
        metrics={
            "cape_cover_frac": cape_cover,
            "rad_cover_frac": rad_cover,
            "htop_dc_cover_frac": htop_dc_cover,
            "wet_frac": wet_frac,
            "cape_max_jkg": cape_max,  # reference only, not used by the gate
            "lead_range": list(lead_range),
            "bbox": list(bbox.bounds),
            "thresholds": {
                "cape_cell": CAPE_CELL_THRESH,
                "cape_cover_frac": CAPE_COVER_FRAC,
                "rad_cell": RAD_CELL_THRESH,
                "rad_cover_frac": RAD_COVER_FRAC,
                "precip_wet_cell_mm": PRECIP_WET_CELL_MM,
                "precip_wet_max_frac": PRECIP_WET_MAX_FRAC,
                "htop_dc_cell_min_m": HTOP_DC_CELL_MIN_M,
                "htop_dc_cover_frac": HTOP_DC_COVER_FRAC,
            },
        },
    )
