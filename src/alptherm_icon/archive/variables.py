"""Tier 1 / Tier 2 variable manifests for the M0 archive (plan §4.1, §9.2).

Tier 1 is the unconditional daily Mitschnitt — the surface and
diagnostic fields that drive Komp. C's forcing and Komp. E's basis
validation. Every value here costs a single GRIB2 file per (lead, var).

Tier 2 is the conditional full-3D snapshot: native model-level T, QV,
U, V, W (×65 levels × N leads) plus HHL once. Triggered by
:mod:`alptherm_icon.archive.trigger` when the same run smells like a
gut day. Each variable here is ~65× the cost of a Tier-1 entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from alptherm_icon.icon_pipeline.icon import ICON_D2_N_FULL_LEVELS, IconD2File

# Tier 1 — full §4.1 surface / diagnostic set (15 vars).
# Kept as lowercase DWD URL tokens; order is documentation, not requirement.
TIER1_SURFACE_VARS: tuple[str, ...] = (
    # Bodennahe Größen
    "t_2m",
    "td_2m",
    "t_g",
    "relhum_2m",
    # Strahlung (akkumuliert)
    "asob_s",
    "athb_s",
    # Wärmeströme (akkumuliert)
    "ashfl_s",
    "alhfl_s",
    # Bewölkung
    "clct_mod",
    # Konvektions-Diagnostik
    "hbas_sc",  # base of shallow convection [m MSL]
    "htop_sc",  # top of shallow convection [m MSL]
    "htop_dc",  # top of dry convection [m MSL] — direct Blue-Day proxy
    # Stabilität / CBL
    "cape_ml",
    "cin_ml",  # Convective Inhibition [J/kg] — pairs with cape_ml
    "hzerocl",  # Height of 0 °C level [m MSL] — Schauer-/Gewitter-Indikator
    "mh",  # Mixed Layer Depth [m AGL] — DWD's CBL-height field for ICON-D2
    #      (ICON-D2 does not publish `hpbl`; `mh` carries the same meaning)
    # Niederschlag (Gewitter-Ausschluss + Tier-2-Trigger)
    "tot_prec",
)
# NB: plan §4.1 also lists HBAS_CON, HTOP_CON, HPBL, LCL_ML — none of
# these are published by DWD for ICON-D2 (only ICON-EU). Substitutes
# used here: HPBL → MH (CBL-height equivalent); HBAS_CON dropped in
# favour of HTOP_DC for the Blue-Day trigger; LCL_ML covered indirectly
# by HBAS_SC. HTOP_CON has no ICON-D2 equivalent and is omitted.

# Subset of Tier 1 the diagnostic trigger needs (§9.2). Kept here so the
# trigger can self-fetch this minimum even when the full tier1 archive
# for the decision run hasn't been written yet — that decouples the
# 12-UTC trigger cron from the tier1-09 cron's completion time.
#
# htop_dc serves the Blue-Day positive-indicator role (Trockenkonvektion
# diagnosed = real thermals); the originally-planned hbas_con doesn't
# exist on opendata for ICON-D2 (only ICON-EU publishes it).
TRIGGER_VARS: tuple[str, ...] = ("cape_ml", "asob_s", "htop_dc", "tot_prec")

# Tier 2 — native model-level profile vars. HHL is time-invariant
# (00 UTC init only) and handled separately by the orchestrator.
# TKE: Turbulent Kinetic Energy — model-level only; per plan §4.1 used
# as a cross-check for our own CBL-height diagnostic (Komp. C).
TIER2_PROFILE_VARS: tuple[str, ...] = ("t", "qv", "u", "v", "w", "tke")

# Lead window we actually keep at Tier 2. Tier 2 is always pulled from
# the 06-UTC-Lauf (§9.2 Sweet Spot), so leads 1..12 cover 07..18 UTC —
# the full Tagesthermik window with margin for Onset and dissipation.
TIER2_LEAD_RANGE: tuple[int, int] = (1, 12)


@dataclass(frozen=True)
class ArchiveJob:
    """A list of IconD2File specs that together make up one archive tier."""

    tier: str  # "tier1" | "tier2"
    init: datetime
    specs: tuple[IconD2File, ...]

    def __len__(self) -> int:
        return len(self.specs)


def tier1_specs(init: datetime, lead_max: int = 48) -> tuple[IconD2File, ...]:
    """All Tier-1 IconD2File specs for one init (every surface var × every lead).

    ``tot_prec`` is accumulated-from-init and has no value at lead 0
    (DWD publishes it from lead 1), but the downloader returns ``None``
    on 404 and the manifest records the miss — no special-casing here.
    """
    specs: list[IconD2File] = []
    for lead in range(0, lead_max + 1):
        for var in TIER1_SURFACE_VARS:
            specs.append(IconD2File(init=init, lead_h=lead, var=var))
    return tuple(specs)


def tier2_specs(
    init: datetime,
    lead_range: tuple[int, int] = TIER2_LEAD_RANGE,
    levels: Iterable[int] | None = None,
    include_hhl: bool = True,
) -> tuple[IconD2File, ...]:
    """All Tier-2 IconD2File specs (var × lead × level), plus HHL once."""
    lead_lo, lead_hi = lead_range
    if levels is None:
        levels = range(1, ICON_D2_N_FULL_LEVELS + 1)
    levels = tuple(levels)
    specs: list[IconD2File] = []
    for lead in range(lead_lo, lead_hi + 1):
        for var in TIER2_PROFILE_VARS:
            for k in levels:
                specs.append(
                    IconD2File(
                        init=init, lead_h=lead, var=var, level_type="model-level", level=k
                    )
                )
    if include_hhl:
        # HHL is published only with the 00 UTC init of the same day.
        hhl_init = init.replace(hour=0)
        for k in range(1, ICON_D2_N_FULL_LEVELS + 2):  # 66 half-levels
            specs.append(
                IconD2File(
                    init=hhl_init,
                    lead_h=0,
                    var="hhl",
                    level_type="time-invariant",
                    level=k,
                )
            )
    return tuple(specs)


def estimate_tier2_file_count(
    lead_range: tuple[int, int] = TIER2_LEAD_RANGE,
    n_levels: int = ICON_D2_N_FULL_LEVELS,
    include_hhl: bool = True,
) -> int:
    n_leads = lead_range[1] - lead_range[0] + 1
    n = n_leads * len(TIER2_PROFILE_VARS) * n_levels
    if include_hhl:
        n += n_levels + 1
    return n
