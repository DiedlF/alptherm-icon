"""Komponente C — Modellkern (1D-Konvektion, plan §5).

Liechti's physics preserved (volume effect, AHD, 1D energy balance), but
fed by ICON diagnostics instead of 1994 empirical parameterisations:
P = ASOB_S + ATHB_S, P_sens/P_lat from surface flux fields, T_S = T_G,
large-scale subsidence from ICON W.

Internal time step ~2 min (§5.2). Output v(z,t) per region in 100 m × 30 min
bins compatible with IGC binning.

v0.1: bulk mixed-layer encroachment (single z_i evolution); no AHD-coupled
bin-wise parcel theory yet. See model/mixed_layer.py.
"""

from alptherm_icon.model.mixed_layer import (
    CblStep,
    cbl_top_from_cumulative_heat,
    evolve_mixed_layer,
)
from alptherm_icon.model.thermo import (
    C_P,
    G,
    KAPPA,
    P_0,
    R_D,
    potential_temperature,
    standard_pressure,
    temperature_from_theta,
    w_star,
)

__all__ = [
    "C_P",
    "CblStep",
    "G",
    "KAPPA",
    "P_0",
    "R_D",
    "cbl_top_from_cumulative_heat",
    "evolve_mixed_layer",
    "potential_temperature",
    "standard_pressure",
    "temperature_from_theta",
    "w_star",
]
