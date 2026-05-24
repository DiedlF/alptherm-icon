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
than miss. Tier 2 fires if any of:

- ``cape_ml`` spatial-max over the Alpen-Bbox exceeds CAPE_THRESH on
  any lead in the convective window;
- daily-max ``asob_s`` exceeds RAD_THRESH — catches Blue Days that
  have low CAPE but plenty of forcing;
- ``tot_prec`` accumulated over the convective window stays below
  PRECIP_DRY_MAX *and* ``htop_dc`` (top of dry convection) reaches
  HTOP_DC_BLUE_MIN_M — i.e. trocken, aber meßbare Trockenkonvektion.
  That is the textbook Blue-Day pattern §8 calls most under-represented
  in IGC.

The decision logs the evaluated metrics so we can audit / re-tune
thresholds later from the manifest alone.
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

CAPE_THRESH = 100.0  # J/kg — generous; Blue Days easily sit at 50–150
# ASOB_S note: ICON publishes this as average-since-init [W/m²], not the
# instantaneous SW. From a 09 UTC decision run, lead 1 is an ~1 h average
# (10 UTC, near-instantaneous), lead 6 is a 6 h average (09–15 UTC,
# diluted). The spatial-max over leads therefore weights lead 1–3 most
# strongly — fine for a clear-sky / forcing proxy, but worth re-tuning
# the threshold if we ever switch to a different decision run.
RAD_THRESH = 600.0  # W/m² — average since 09 UTC init, clear-sky proxy
PRECIP_DRY_MAX = 1.0  # mm — accumulated over the convective window
# Blue-Day positive indicator: top of dry convection [m MSL]. 2500 m is
# generous (Voralpen-Boden ~500 m → 2000 m AGL, Talsohle ~1000 m →
# 1500 m AGL — both qualify as real thermals). Re-tune after we've seen
# IGC overlap statistics.
HTOP_DC_BLUE_MIN_M = 2500.0


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


def _bbox_spatial_stat(ds: xr.Dataset, stat: str = "max") -> float:
    """Compute a spatial statistic over the Alpen-Bbox for a single-var Dataset."""
    sub = subset_to_bbox(ds, ALPEN_BBOX)
    da = sub[next(iter(sub.data_vars))]
    arr = np.asarray(da.values, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("nan")
    if stat == "max":
        return float(arr.max())
    if stat == "min":
        return float(arr.min())
    if stat == "mean":
        return float(arr.mean())
    if stat == "coverage_nonzero":
        return float((arr > 0).mean())
    raise ValueError(f"unknown stat {stat!r}")


def evaluate(
    grib_paths: dict[tuple[str, int], Path],
    lead_range: tuple[int, int] = TRIGGER_LEAD_RANGE,
    bbox: BBox = ALPEN_BBOX,
    cape_thresh: float = CAPE_THRESH,
    rad_thresh: float = RAD_THRESH,
    precip_dry_max: float = PRECIP_DRY_MAX,
) -> TriggerDecision:
    """Decide Tier 2 from already-downloaded Tier 1 GRIB2 paths.

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

    cape_max = float("nan")
    rad_max = float("nan")
    htop_dc_max_m = float("nan")

    cape_per_lead: list[float] = []
    rad_per_lead: list[float] = []
    htop_dc_per_lead: list[float] = []

    for lead in leads:
        for var, accumulate in (
            ("cape_ml", cape_per_lead),
            ("asob_s", rad_per_lead),
            ("htop_dc", htop_dc_per_lead),
        ):
            path = grib_paths.get((var, lead))
            if path is None or not path.exists():
                continue
            ds = _open_grib(path, step_h=lead)
            if ds is None:  # corrupt / truncated GRIB — skip, don't crash
                continue
            try:
                accumulate.append(_bbox_spatial_stat(ds, "max"))
            finally:
                ds.close()

    # TOT_PREC is accumulated-from-init, so the window total is
    # max(lead_hi) - max(lead_lo - 1). At lead 0 nothing has fallen yet
    # — DWD doesn't publish a lead-0 file, but the value is semantically
    # zero, so short windows starting at lead 1 still get a defined diff.
    def _accum(lead: int) -> float:
        if lead == 0:
            return 0.0
        p = grib_paths.get(("tot_prec", lead))
        if p is None or not p.exists():
            return float("nan")
        ds = _open_grib(p, step_h=lead)
        if ds is None:
            return float("nan")
        try:
            return _bbox_spatial_stat(ds, "max")
        finally:
            ds.close()

    precip_lead_lo = _accum(max(lead_lo - 1, 0))
    precip_lead_hi = _accum(lead_hi)
    if np.isfinite(precip_lead_lo) and np.isfinite(precip_lead_hi):
        precip_window = precip_lead_hi - precip_lead_lo
    else:
        precip_window = float("nan")

    def _finite_max(xs: list[float]) -> float:
        finite = [v for v in xs if np.isfinite(v)]
        return max(finite) if finite else float("nan")

    cape_max = _finite_max(cape_per_lead)
    rad_max = _finite_max(rad_per_lead)
    htop_dc_max_m = _finite_max(htop_dc_per_lead)

    reasons: list[str] = []
    if np.isfinite(cape_max) and cape_max > cape_thresh:
        reasons.append(f"cape_max={cape_max:.0f}>{cape_thresh:.0f}")
    if np.isfinite(rad_max) and rad_max > rad_thresh:
        reasons.append(f"rad_max={rad_max:.0f}>{rad_thresh:.0f}")
    if (
        np.isfinite(precip_window)
        and precip_window < precip_dry_max
        and np.isfinite(htop_dc_max_m)
        and htop_dc_max_m > HTOP_DC_BLUE_MIN_M
    ):
        reasons.append(
            f"blue_day(precip={precip_window:.1f}<{precip_dry_max:.1f},"
            f"htop_dc={htop_dc_max_m:.0f}>{HTOP_DC_BLUE_MIN_M:.0f})"
        )

    fire = bool(reasons)
    return TriggerDecision(
        fire=fire,
        reason=" OR ".join(reasons) if reasons else "no_threshold_met",
        metrics={
            "cape_max": cape_max,
            "rad_max": rad_max,
            "precip_window_mm": precip_window,
            "htop_dc_max_m": htop_dc_max_m,
            "lead_range": list(lead_range),
            "bbox": list(bbox.bounds),
            "thresholds": {
                "cape": cape_thresh,
                "rad": rad_thresh,
                "precip_dry_max": precip_dry_max,
                "htop_dc_blue_min_m": HTOP_DC_BLUE_MIN_M,
            },
        },
    )
