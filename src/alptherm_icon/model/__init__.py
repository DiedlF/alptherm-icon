"""Komponente C — Modellkern (1D-Konvektion, plan §5).

Liechti's physics preserved (volume effect, AHD, 1D energy balance), but
fed by ICON diagnostics instead of 1994 empirical parameterisations:
P = ASOB_S + ATHB_S, P_sens/P_lat from surface flux fields, T_S = T_G,
large-scale subsidence from ICON W.

Internal time step ~2 min (§5.2). Output v(z,t) per region in 100 m × 30 min
bins compatible with IGC binning.
"""
