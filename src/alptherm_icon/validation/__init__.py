"""Komponente E — Validierung und Parametertuning (plan §7).

Three-layer validation: classify day type from ICON (Cu / Blue / thunder)
and tune against the appropriate target — HBAS_SC + IGC max heights for
Cu days, HTOP_DC + IGC max for blue days, exclude thunder days.

Avoid circularity: base height tuned primarily against ICON diagnostics
(spatially complete), IGC max heights only as plausibility check.

Remaining tuning parameters after ICON migration:
  ΔT₀, P₀, E_n0, D_c0, r (wind reduction), Bart-Skalierung.
Optimisation via grid search or scikit-optimize / optuna.
"""
