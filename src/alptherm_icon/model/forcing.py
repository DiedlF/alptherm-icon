"""Surface-flux forcing for the model kernel (Komp. C v0.2).

ICON-D2 surface flux fields (ASOB_S, ATHB_S, ASHFL_S, ALHFL_S) are published as
*averages since model initialisation* (W/m², GRIB ``stepType=avg``) — **not**
instantaneous values and **not** time-accumulated J/m². The kernel needs the
instantaneous mean flux over each forecast interval, recovered by de-averaging:

    F(t_{i-1} → t_i) = (mean_i · t_i − mean_{i-1} · t_{i-1}) / (t_i − t_{i-1})

where ``t`` is the lead time in seconds since init. ``mean_i · t_i`` is the
integral of the flux from init to lead ``t_i``; differencing two such integrals
and dividing by the interval gives the interval-mean flux. The v0.1 proxy fed
the running mean directly, which lags solar noon and understates the midday
flux (empirically ~570 W/m² peak net SW de-averaged vs. ~250 W/m² running mean).

Sign convention: DWD surface turbulent fluxes are downward-positive. An upward
flux that heats the CBL is therefore *negative* ASHFL_S/ALHFL_S, so the model's
positive-upward fluxes are ``P_sens = −ASHFL_S`` and ``P_lat = −ALHFL_S``. The
radiation balance ``ASOB_S + ATHB_S`` is already a net downward flux and needs
no sign flip.
"""

from __future__ import annotations

import numpy as np


def deaverage_since_init(
    mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
) -> np.ndarray:
    """De-average a "mean since init" field to per-interval instantaneous flux.

    Args:
        mean_since_init: running mean since init at each lead [W/m²]. Index 0 is
            the init time (lead 0), where the mean is the undefined ``∫/0`` — its
            value is ignored (the integral at lead 0 is 0 regardless).
        lead_seconds: lead time in seconds since init, strictly ascending, same
            shape. ``lead_seconds[0]`` must be 0 (the init time).

    Returns:
        Array of the same shape. Element ``i`` is the mean flux over the interval
        ``(lead[i-1], lead[i]]``; element 0 is 0 (no interval precedes init). A
        NaN in ``mean_since_init[i]`` propagates to intervals ``i`` and ``i+1``.
    """
    mean = np.asarray(mean_since_init, dtype=np.float64)
    t = np.asarray(lead_seconds, dtype=np.float64)
    if mean.shape != t.shape:
        raise ValueError("mean_since_init and lead_seconds must have the same shape")
    if mean.ndim != 1 or mean.size < 2:
        raise ValueError("need a 1-D array of at least two lead times")
    if t[0] != 0:
        raise ValueError("lead_seconds[0] must be 0 (the init time)")
    if np.any(np.diff(t) <= 0):
        raise ValueError("lead_seconds must be strictly ascending")

    cum = mean * t  # ∫₀ᵗ F dt' [W/m²·s]
    cum[0] = 0.0  # integral at lead 0 is 0 even if mean[0] is NaN/undefined
    flux = np.empty_like(mean)
    flux[0] = 0.0
    flux[1:] = np.diff(cum) / np.diff(t)
    return flux


def sensible_flux_proxy(
    asob_s_mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
    fraction: float,
) -> np.ndarray:
    """v0.1 proxy: a fixed fraction of de-averaged net surface shortwave.

    ``ASOB_S`` is a net *downward* flux, so no sign flip is needed.
    """
    return deaverage_since_init(asob_s_mean_since_init, lead_seconds) * fraction


def turbulent_flux_from_icon(
    flux_mean_since_init: np.ndarray,
    lead_seconds: np.ndarray,
) -> np.ndarray:
    """Model-positive (upward, CBL-heating) turbulent flux from a DWD field.

    Applies de-averaging and the downward-positive→upward-positive sign flip, so
    ``ASHFL_S``/``ALHFL_S`` become ``P_sens``/``P_lat`` as the kernel expects.
    """
    return -deaverage_since_init(flux_mean_since_init, lead_seconds)
